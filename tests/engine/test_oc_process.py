"""`OcProcess` — процесс OpenCode с поверхностью и потоком `claude -p` stream-json (Ф16, кусок 16-5, П12 с
уточнениями круга 2). Сервер — ПОДДЕЛЬНЫЙ (`fake_opencode.py`); проверяется форма сообщений, которую ждут хост
hub-work и оркестратор, и путь карточек (ask, вопросы, подагенты) — задел для 16-6."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from hub.engine import oc_config
from hub.engine.oc_process import OcProcess, OcSpec, OcStartError, _TurnState, question_answers
from hub.engine.oc_turn import Allow, Ask, Deny


class Harness:
    def __init__(self, fake, *, decide=None, ask=True, permission=None, strict_mcp=None) -> None:
        self.fake = fake
        self.messages: list[dict] = []
        self.requests: list = []
        oc_config.write(fake.config_path, fake.config())

        def default_decide(req, payload):
            self.requests.append((req, payload))
            return Allow()

        self.spec = OcSpec(
            binary=str(fake.binary),
            cwd=str(fake.workdir),
            env=fake.env(),
            config_path=fake.config_path,
            log_file=fake.tmp / "logs" / "proc-1.log",
            agent="hub-test",
            permission=permission or oc_config.PERM_ASK_ALL,
            model=("deepseek", "deepseek-flash"),
            variant="max",
            system="правила хаба",
            title="тест",
            permission_mode="bypassPermissions",
            tools=("Bash", "Read"),
            decide=decide or default_decide,
            ask=ask,
            strict_mcp=strict_mcp,
            poll_s=0.2,
            stop_grace_s=1.0,
        )
        self.process = OcProcess(self.spec, on_message=self.on_message)

    async def on_message(self, message: dict) -> None:
        self.messages.append(message)

    def of(self, kind: str, subtype: str | None = None) -> list[dict]:
        return [
            m
            for m in self.messages
            if m.get("type") == kind and (subtype is None or m.get("subtype") == subtype)
        ]

    async def until(self, predicate, limit_s: float = 10.0, what: str = "условие"):
        deadline = time.monotonic() + limit_s
        while True:
            value = predicate()
            if value:
                return value
            if time.monotonic() > deadline:
                raise AssertionError(f"не дождались: {what}; сообщения: {self.messages}")
            await asyncio.sleep(0.02)

    async def say(self, text: str, uid: str = "u-1") -> bool:
        line = {
            "type": "user",
            "session_id": "",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
            "parent_tool_use_id": None,
            "uuid": uid,
        }
        return await self.process.send(line)


@pytest.fixture
async def harness(fake_oc):
    made: list[Harness] = []

    def make(**kw) -> Harness:
        h = Harness(fake_oc, **kw)
        made.append(h)
        return h

    yield make
    for h in made:
        await h.process.kill_tree(grace_s=1.0)
        await h.process.aclose()


def _blocks(messages: list[dict], kind: str) -> list[dict]:
    return [b for m in messages if m.get("type") == kind for b in m["message"]["content"]]


async def test_initialize_after_control_and_turn_shape(fake_oc, harness):
    """`initialize` — ответ ПОСЛЕ возврата `control()`; ход: `system/init` первым, текст,
    `tool_use`/`tool_result` в формате Claude, `result` с итогом."""
    fake_oc.scenario(
        turns=[
            [{"text": "Смотрю."}, {"ask": {"tool": "bash", "args": {"command": "ls"}}}, {"text": "Готово."}]
        ]
    )
    h = harness()
    sid = await h.process.start()
    assert sid.startswith("ses_") and not h.process.resumed
    rid = await h.process.control("initialize")
    assert h.messages == []  # ответ ещё не выдан — хост успеет зарегистрировать ожидание
    await h.until(lambda: h.of("control_response"), what="ответ initialize")
    response = h.of("control_response")[0]["response"]
    assert response == {
        "subtype": "success",
        "request_id": rid,
        "response": {"current_permission_mode": "bypassPermissions"},
    }
    assert await h.say("начни")
    await h.until(lambda: h.of("result"), what="result")
    kinds = [m["type"] for m in h.messages[1:]]
    assert kinds[0] == "system" and h.messages[1]["subtype"] == "init"
    init = h.messages[1]
    assert init["session_id"] == sid and init["tools"] == ["Bash", "Read"]
    assert init["permissionMode"] == "bypassPermissions"
    assert kinds[-1] == "result"
    blocks = _blocks(h.messages, "assistant")
    assert blocks[0] == {"type": "text", "text": "Смотрю."}
    assert blocks[1]["type"] == "tool_use" and blocks[1]["name"] == "Bash"
    assert blocks[1]["input"] == {"command": "ls"}
    assert blocks[2] == {"type": "text", "text": "Готово."}
    results = _blocks(h.messages, "user")
    assert results[0]["type"] == "tool_result" and results[0]["tool_use_id"] == blocks[1]["id"]
    assert results[0]["is_error"] is False
    result = h.of("result")[0]
    assert result["subtype"] == "success" and not result["is_error"] and result["result"] == "Готово."
    assert result["session_id"] == sid and result["num_turns"] == 1 and result["total_cost_usd"] == 0.001
    assert result["terminal_reason"] == "completed" and result["permission_denials"] == []
    # решение — по вызову в формате Claude; ответ — `once`
    req, payload = h.requests[0]
    assert req.tool == "bash" and payload["tool_name"] == "Bash"
    assert [r["reply"] for r in fake_oc.jsonl("replies.jsonl")] == ["once"]
    prompt = next(c for c in fake_oc.jsonl("calls.jsonl") if c["path"].endswith("/prompt_async"))
    assert prompt["body"]["system"] == "правила хаба" and prompt["body"]["agent"] == "hub-test"
    assert prompt["body"]["variant"] == "max"


async def test_ask_becomes_card_allow_and_deny(fake_oc, harness):
    """«ask» -> `control_request can_use_tool` (id — id запроса OpenCode); «разрешить» -> `once`,
    «отказать» -> `reject` с текстом владельца и отказ в `permission_denials`."""
    fake_oc.scenario(
        turns=[
            [
                {"ask": {"tool": "bash", "args": {"command": "git push"}}},
                {"ask": {"tool": "bash", "args": {"command": "git push --force"}}},
                {"text": "ок"},
            ]
        ]
    )
    h = harness(decide=lambda req, payload: Ask({"reason": "push под кнопкой"}))
    await h.process.start()
    await h.say("пушни")
    await h.until(lambda: h.of("control_request"), what="карточка")
    first = h.of("control_request")[0]
    request = first["request"]
    assert first["request_id"].startswith("per_")
    assert request["subtype"] == "can_use_tool" and request["tool_name"] == "Bash"
    assert request["input"] == {"command": "git push"} and request["decision_reason"] == "push под кнопкой"
    assert await h.process.respond(first["request_id"], {"behavior": "allow", "updatedInput": {}})
    assert not await h.process.respond(first["request_id"], {"behavior": "allow"})  # второй раз — нечего
    await h.until(lambda: len(h.of("control_request")) == 2, what="вторая карточка")
    second = h.of("control_request")[1]
    await h.process.send(
        {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": second["request_id"],
                "response": {"behavior": "deny", "message": "не надо force"},
            },
        }
    )
    await h.until(lambda: h.of("result"), what="result")
    replies = [(r["reply"], r["message"]) for r in fake_oc.jsonl("replies.jsonl")]
    assert replies == [("once", None), ("reject", "не надо force")]
    denials = h.of("result")[0]["permission_denials"]
    assert [d["tool_input"] for d in denials] == [{"command": "git push --force"}]


async def test_question_card_answers_labels(fake_oc, harness):
    """Вопрос модели (F21) -> карточка `AskUserQuestion`; ответ `{вопрос: ответ}` -> метки OpenCode."""
    questions = [
        {
            "question": "Какую ветку?",
            "header": "Ветка",
            "options": [{"label": "main", "description": ""}, {"label": "dev", "description": ""}],
        },
        {
            "question": "Что проверить?",
            "header": "Проверки",
            "multiple": True,
            "options": [{"label": "тесты", "description": ""}, {"label": "линт", "description": ""}],
        },
    ]
    fake_oc.scenario(turns=[[{"question": {"questions": questions}}, {"text": "ок"}]])
    h = harness()
    await h.process.start()
    await h.say("спроси")
    await h.until(lambda: h.of("control_request"), what="вопрос")
    card = h.of("control_request")[0]
    assert card["request"]["tool_name"] == "AskUserQuestion"
    shown = card["request"]["input"]["questions"]
    assert shown[0]["question"] == "Какую ветку?" and shown[0]["options"][1]["label"] == "dev"
    assert shown[1]["multiSelect"] is True
    answers = {"Какую ветку?": "dev", "Что проверить?": "тесты, линт"}
    await h.process.respond(card["request_id"], {"behavior": "allow", "updatedInput": {"answers": answers}})
    await h.until(lambda: h.of("result"), what="result")
    reply = next(r for r in fake_oc.jsonl("replies.jsonl") if r["kind"] == "question")
    assert reply["reply"] == "reply" and reply["answers"] == [["dev"], ["тесты", "линт"]]


def test_question_answers_free_text():
    q = [{"question": "Имя?", "options": [{"label": "a"}], "multiple": True}]
    assert question_answers(q, {"Имя?": "свой, ответ"}) == [["свой, ответ"]]
    assert question_answers(q, {}) == [[]]


async def test_interrupt_cancels_card_and_aborts(fake_oc, harness):
    """«Стоп» при висящей карточке: `control_cancel_request`, `abort`, `result` с
    `terminal_reason: aborted`."""
    fake_oc.scenario(turns=[[{"ask": {"tool": "bash", "args": {"command": "git push"}}}, {"hang": True}]])
    h = harness(decide=lambda req, payload: Ask({}))
    await h.process.start()
    await h.say("пушни")
    await h.until(lambda: h.of("control_request"), what="карточка")
    rid = h.of("control_request")[0]["request_id"]
    await h.process.control("interrupt", cancel_queued=True)
    await h.until(lambda: h.of("result"), what="result")
    assert [m["request_id"] for m in h.of("control_cancel_request")] == [rid]
    assert any(c["path"].endswith("/abort") for c in fake_oc.jsonl("calls.jsonl"))
    result = h.of("result")[0]
    assert result["terminal_reason"] == "aborted" and result["subtype"] == "error_during_execution"
    assert not await h.process.respond(rid, {"behavior": "allow"})


async def test_child_and_foreign_permissions(fake_oc, harness):
    """П5: запрос подагента (потомка) идёт через решение с `child=True`; чужая сессия — без ответа."""
    fake_oc.scenario(
        turns=[
            [
                {"ask": {"tool": "read", "args": {"filePath": "/etc/hosts"}}, "child": True},
                {"ask": {"tool": "read", "args": {"filePath": "/x"}}, "foreign": True, "wait_s": 1.0},
                {"text": "ок"},
            ]
        ]
    )
    h = harness()
    await h.process.start()
    await h.say("разведай")
    await h.until(lambda: h.of("result"), what="result")
    assert [(req.child, payload["tool_input"]) for req, payload in h.requests] == [
        (True, {"file_path": "/etc/hosts"})
    ]
    replies = fake_oc.jsonl("replies.jsonl")
    assert [(r["child"], r["foreign"], r["reply"]) for r in replies] == [
        (True, False, "once"),
        (True, True, None),
    ]


async def test_prompt_into_busy_session_same_turn(fake_oc, harness):
    """F10: реплика посреди хода — в очередь того же хода: один `system/init`, один `result` (итог — ответ на
    последнюю реплику); повтор `uuid` второй раз не шлётся."""
    fake_oc.scenario(turns=[[{"text": "раз"}, {"sleep": 0.6}, {"text": "два"}], [{"text": "три"}]])
    h = harness()
    await h.process.start()
    await h.say("первая", uid="u-1")
    await h.until(lambda: _blocks(h.messages, "assistant"), what="первый текст")
    assert await h.say("вторая", uid="u-2")
    assert await h.say("вторая", uid="u-2")  # повтор uuid
    await h.until(lambda: h.of("result"), what="result")
    await asyncio.sleep(0.5)
    assert len(h.of("system", "init")) == 1 and len(h.of("result")) == 1
    assert [b["text"] for b in _blocks(h.messages, "assistant")] == ["раз", "два", "три"]
    assert h.of("result")[0]["result"] == "три" and h.of("result")[0]["num_turns"] == 2
    prompts = [c for c in fake_oc.jsonl("calls.jsonl") if c["path"].endswith("/prompt_async")]
    assert len(prompts) == 2


async def test_close_stdin_idle_and_after_turn(fake_oc, harness):
    """`close_stdin` простаивающего — сервер гасится сразу; во время хода — после `result`."""
    fake_oc.scenario(turns=[[{"text": "раз"}, {"sleep": 0.5}, {"text": "два"}]])
    h = harness()
    await h.process.start()
    await h.say("давай")
    await h.until(lambda: _blocks(h.messages, "assistant"), what="ход идёт")
    await h.process.close_stdin()
    assert not await h.say("ещё", uid="u-2")  # как закрытый stdin
    assert await h.process.wait(10)
    assert h.of("result") and h.of("result")[0]["result"] == "два"
    assert h.process.proc.returncode is not None and h.process.tree() == []

    h2 = harness()
    await h2.process.start()
    await h2.process.close_stdin()
    assert await h2.process.wait(10)


async def test_start_checks_agent_and_mcp(fake_oc, harness):
    """П8: права агента не совпали — отказ запуска; строгий набор MCP без MCP хаба — отказ; сервер погашен."""
    h = harness(permission=oc_config.PERM_ORCH)
    with pytest.raises(OcStartError, match="не совпали"):
        await h.process.start()
    assert not h.process.server.alive()
    h2 = harness(strict_mcp=frozenset({"hub"}))
    with pytest.raises(OcStartError, match="MCP хаба в OpenCode не подключился"):
        await h2.process.start()
    assert not h2.process.server.alive()


async def test_limit_error_rate_limit_event(fake_oc, harness):
    """Лимит провайдера -> `system/engine_error` + `rate_limit_event` (`rejected`, `engine: opencode`,
    `resetsAt` ≈ сейчас + 30 мин) + `result` с ошибкой."""
    fake_oc.scenario(
        turns=[[{"error": {"name": "APIError", "message": "Insufficient balance", "status": 402}}]]
    )
    h = harness()
    await h.process.start()
    await h.say("давай")
    await h.until(lambda: h.of("result"), what="result")
    error = h.of("system", "engine_error")[0]
    assert error["kind"] == "limit" and "лимит или баланс провайдера deepseek" in error["text"]
    info = h.of("rate_limit_event")[0]["rate_limit_info"]
    assert info["status"] == "rejected" and info["engine"] == "opencode"
    assert info["provider"] == "deepseek"  # пауза — на провайдера модели
    assert abs(info["resetsAt"] - (time.time() + 1800)) < 60
    result = h.of("result")[0]
    assert result["is_error"] and result["subtype"] == "error_engine" and result["terminal_reason"] == "error"


async def test_resume_existing_or_new(fake_oc, harness):
    """F16: `start(resume=ses_…)` продолжает сессию, если OpenCode её знает; иначе — новая."""
    h = harness()
    sid = await h.process.start()
    await h.process.kill_tree(grace_s=1.0)
    h2 = harness()
    assert await h2.process.start(resume=sid) == sid and h2.process.resumed
    await h2.process.kill_tree(grace_s=1.0)
    h3 = harness()
    other = await h3.process.start(resume="ses_nothere0000")
    assert other != "ses_nothere0000" and not h3.process.resumed


async def test_deny_from_decide_goes_to_denials(fake_oc, harness):
    fake_oc.scenario(turns=[[{"ask": {"tool": "bash", "args": {"command": "rm -rf /"}}}, {"text": "ок"}]])
    h = harness(decide=lambda req, payload: Deny("гард: нельзя"))
    await h.process.start()
    await h.say("давай")
    await h.until(lambda: h.of("result"), what="result")
    assert [(r["reply"], r["message"]) for r in fake_oc.jsonl("replies.jsonl")] == [
        ("reject", "гард: нельзя")
    ]
    assert h.of("result")[0]["permission_denials"][0]["tool_name"] == "Bash"
    assert _blocks(h.messages, "user")[0]["is_error"] is True


class _StubClient:
    def __init__(self, messages, status="idle"):
        self._messages = messages
        self._status = status

    async def status_info(self, sid):
        return {"type": self._status}

    async def messages(self, sid):
        return self._messages


def _msg(role: str, created: int, **timing) -> dict:
    info = {"id": f"msg_{role}{created}", "role": role, "time": {"created": created, **timing}}
    return {"info": info, "parts": []}


async def test_check_finish_waits_for_own_reply(fake_oc):
    """Сессия простаивает, а последняя запись — ответ ПРОШЛОГО хода (204 пришёл раньше записи реплики) или
    сама реплика (F10 — ещё не взята): ход не окончен; законченный ответ после реплики — окончен."""
    process = Harness(fake_oc).process
    process.session_id = "ses_stub"
    process._turn = SimpleNamespace(error=None, _note_status=lambda status: None)  # noqa: SLF001
    state = _TurnState(started_ms=1000, started_mono=time.monotonic())
    old = [_msg("user", 500), _msg("assistant", 600, completed=700)]
    process.client = _StubClient(old)
    assert await process._check_finish(state) is None  # noqa: SLF001
    process.client = _StubClient([*old, _msg("user", 1001)])
    assert await process._check_finish(state) is None  # noqa: SLF001
    done = [*old, _msg("user", 1001), _msg("assistant", 1002, completed=1003)]
    process.client = _StubClient(done, status="busy")
    assert await process._check_finish(state) is None  # noqa: SLF001
    process.client = _StubClient(done)
    messages, error = await process._check_finish(state)  # noqa: SLF001
    assert error is None and [m["info"]["time"]["created"] for m in messages] == [1001, 1002]
