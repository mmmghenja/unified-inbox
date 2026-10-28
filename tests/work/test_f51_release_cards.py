"""Ф51-1 B (docs/architecture.md): снятая галочка «Автопилота» закрывает уже открытую карточку
сессии хаба — только ту, на которую хост теперь ответил бы сам (`rule in auto_free and answerable and alive`
по повторной проверке запроса). Стенд и помощники — из test_held_autopilot.py."""

import asyncio

from hub.agent.core_api import CoreUnavailable
from hub.core import held_work
from hub.work import guard
from hub.work import held as held_mod

from .test_held_autopilot import QUESTION, _bash, _free, _hold, _requests
from .test_held_host import _events, _running, _session, _turn_done

AUTO_BY = {"auto": True, "role": "автопилот"}
PUSH_MARKED = "гард хаба: push — решает владелец\n[hub-rule:push]"
FOREIGN = _bash("make test", reason_type="hook", reason="общий гард: похоже на секрет")


def _row(env, card_id: int) -> dict:
    with env.db.reader() as conn:
        return held_work.request_dict(held_work._request(conn, card_id)[0])  # noqa: SLF001


async def _card(env, host, sid) -> dict:
    async def pending():
        return [r for r in await _requests(env, sid) if r["status"] == "pending"]

    [request] = await env.until(host, pending, what="карточка")
    return request


async def _quiet(env, host, n: int = 4) -> None:
    for _ in range(n):
        await env.cycle(host)


async def _decisions(env, sid) -> list[dict]:
    return [e for e in await _events(env, sid) if e["kind"] == "decision"]


async def test_guard_card_is_closed_when_its_switch_and_foreign_hook_are_lifted(env):
    """Карточка собственного вердикта гарда (запрос пришёл от хука с меткой гарда хаба): снят только её ключ
    — за запросом мог стоять и чужой хук, карточка остаётся; снят и `foreign_hook` — хост отвечает сам."""
    env.scenario(turns=[[_bash("git push", reason_type="hook", reason=PUSH_MARKED), {"text": "готово"}]])
    host = await env.started()
    sid, _, _ = await _session(env, mode="auto")
    await _running(env, host, sid)
    card = await _card(env, host, sid)
    assert card["rule"] == "push"
    await _free(env, "push")
    await _quiet(env, host)
    assert env.responses() == [] and _row(env, card["id"])["status"] == "pending"

    await _free(env, "foreign_hook")
    await env.until(host, lambda: env.responses(), what="хост ответил сам")
    [response] = [r["response"] for r in env.responses()]
    assert response["behavior"] == "allow" and response["updatedInput"] == {"command": "git push"}
    await _turn_done(env, host, sid)
    row = _row(env, card["id"])
    assert row["status"] == "allowed" and row["decided_by"] == AUTO_BY and row["delivered"] is True
    [decision] = await _decisions(env, sid)
    assert decision["summary"] == "галочка снята — разрешено само: Bash"
    assert await _requests(env, sid) == [] or all(r["status"] != "pending" for r in await _requests(env, sid))
    assert len(env.responses()) == 1 and len(env.calls()) == 1  # без перезапуска и без второго ответа
    log = (env.data_dir / "work" / "guard.log").read_text(encoding="utf-8")
    assert log.count("\tauto\tавтопилот: foreign_hook (галочка снята — карточка закрыта)") == 1


async def test_foreign_hook_card_is_closed_when_the_switch_is_lifted(env):
    env.scenario(turns=[[FOREIGN, {"text": "готово"}]])
    host = await env.started()
    sid, _, _ = await _session(env, mode="auto")
    await _running(env, host, sid)
    card = await _card(env, host, sid)
    assert card["rule"] == "foreign_hook"
    await _free(env, "push")  # чужой ключ карточку не закрывает
    await _quiet(env, host)
    assert env.responses() == []
    await _free(env, "foreign_hook")
    await env.until(host, lambda: env.responses(), what="хост ответил сам")
    assert env.responses()[0]["response"]["behavior"] == "allow"
    await _turn_done(env, host, sid)
    row = _row(env, card["id"])
    assert row["status"] == "allowed" and row["decided_by"] == AUTO_BY
    # позднее решение владельца — «уже решено»
    r = await env.ui("POST", f"/session-requests/{card['id']}/decide", {"decision": "deny"})
    assert r.status_code == 409


async def test_question_card_is_closed_with_the_auto_answer(env):
    env.scenario(turns=[[{"ask": {"tool": "AskUserQuestion", "input": QUESTION}}, {"text": "решила"}]])
    host = await env.started()
    sid, _, _ = await _session(env, mode="auto")
    await _running(env, host, sid)
    card = await _card(env, host, sid)
    assert card["rule"] == "question"
    await _free(env, "question")
    await env.until(host, lambda: env.responses(), what="хост ответил сам")
    [response] = [r["response"] for r in env.responses()]
    assert response["behavior"] == "deny" and response["message"] == held_mod.AUTO_ANSWER_TEXT
    await _turn_done(env, host, sid)
    row = _row(env, card["id"])
    assert row["status"] == "denied" and row["decided_by"] == AUTO_BY and row["delivered"] is True
    assert [e["summary"] for e in await _decisions(env, sid)] == ["галочка снята — вопрос решает сама"]


async def test_doubtful_cards_stay(env):
    """Не закрываются: метка без собственного вердикта хоста, встроенная проверка движка, ключ настроек
    процесса (`PERMISSION_RULES`) — при любом наборе свобод; решает владелец."""
    env.scenario(
        turns=[
            [
                # метку дописал не гард хаба: своя проверка хоста на `make test` молчит
                _bash("make test", reason_type="hook", reason="чужой хук\n[hub-rule:push]"),
                _bash("make lint", reason_type="safetyCheck", reason="движок: опасная команда"),
                {"ask": {"tool": "mcp__claude_ai_Slack__slack_send_message", "input": {"text": "x"}}},
                {"text": "готово"},
            ]
        ]
    )
    host = await env.started()
    sid, _, _ = await _session(env, mode="auto")
    await _running(env, host, sid)
    for n, rule in enumerate(("push", "project_permissions", "mcp_write")):
        card = await _card(env, host, sid)
        assert card["rule"] == rule
        if n == 0:  # галочки снимаются при открытой карточке
            await _free(env, "push", "foreign_hook", "project_permissions", "question", "server")
        await _quiet(env, host)
        runner = host.held.runners[sid]
        assert {"push", "foreign_hook", "project_permissions"} <= runner.auto_free
        assert await runner.release_freed() == 0
        assert len(env.responses()) == n and _row(env, card["id"])["status"] == "pending"
        r = await env.ui("POST", f"/session-requests/{card['id']}/decide", {"decision": "deny"})
        assert r.status_code == 200, r.text
        await env.until(host, lambda n=n: len(env.responses()) == n + 1, what="отказ владельца доставлен")
    await _turn_done(env, host, sid)
    assert [r["response"]["behavior"] for r in env.responses()] == ["deny"] * 3


async def test_failure_card_stays(env, monkeypatch):
    """Сбой собственной проверки (`guard.FAILURE`) — карточка остаётся, что бы ни сняли."""
    env.scenario(turns=[[FOREIGN, {"text": "готово"}]])
    host = await env.started()
    sid, _, _ = await _session(env, mode="auto")
    monkeypatch.setattr(
        held_mod.HeldRunner, "_self_check", lambda self, tool, tool_input: (guard.FAILURE, "гард хаба: сбой")
    )
    await _running(env, host, sid)
    card = await _card(env, host, sid)
    assert card["rule"] == guard.FAILURE
    monkeypatch.undo()  # проверка «починилась» — карточка сбоя всё равно за владельцем
    await _free(env, "foreign_hook", "project_permissions", "push")
    await _quiet(env, host)
    assert await host.held.runners[sid].release_freed() == 0
    assert env.responses() == [] and _row(env, card["id"])["status"] == "pending"
    r = await env.ui("POST", f"/session-requests/{card['id']}/decide", {"decision": "allow"})
    assert r.status_code == 200
    await _turn_done(env, host, sid)


async def test_owner_first_then_no_second_answer(env):
    """Гонка: владелец решил раньше — его решение доставлено, проход по ждущим ничего не шлёт."""
    env.scenario(turns=[[FOREIGN, {"text": "готово"}]])
    host = await env.started()
    sid, _, _ = await _session(env, mode="auto")
    await _running(env, host, sid)
    card = await _card(env, host, sid)
    runner = host.held.runners[sid]
    r = await env.ui("POST", f"/session-requests/{card['id']}/decide", {"decision": "deny"})
    assert r.status_code == 200
    await _free(env, "foreign_hook")
    await env.until(host, lambda: env.responses(), what="решение владельца у процесса")
    await _turn_done(env, host, sid)
    assert await runner.release_freed() == 0
    assert [r["response"]["behavior"] for r in env.responses()] == ["deny"]
    row = _row(env, card["id"])
    assert row["status"] == "denied" and row["decided_by"] is None


async def test_owner_denied_in_core_before_the_host_pass_wins(env):
    """Ревью, находка 1 (B5 «выигрывает первый»): владелец отказал в ядре, хост о решении ещё не знает и уже
    идёт по карточкам с новым набором — ядро отвечает 409, хост движку НЕ отвечает, отказ владельца доходит
    обычной доставкой; карточка не «протухает»."""
    env.scenario(turns=[[FOREIGN, {"sleep": 0.3}, {"text": "готово"}]])
    host = await env.started()
    sid, _, _ = await _session(env, mode="auto")
    await _running(env, host, sid)
    card = await _card(env, host, sid)
    # хост узнал о снятой галочке (apply), но ещё не применил её (это делает следующий tick)
    await _free(env, "foreign_hook")
    data = await host.core.get("/work/host/wait", params=host.wait_params(0))
    await host.apply(data)
    r = await env.ui("POST", f"/session-requests/{card['id']}/decide", {"decision": "deny"})
    assert r.status_code == 200 and _row(env, card["id"])["status"] == "denied"
    await host.tick()  # применяет набор и запускает проход по карточкам — раньше, чем придёт решение
    await asyncio.sleep(0.3)
    assert env.responses() == []  # проход не ответил движку
    await env.until(host, lambda: env.responses(), what="ответ движку")
    await _turn_done(env, host, sid)
    assert [r["response"]["behavior"] for r in env.responses()] == ["deny"]
    row = _row(env, card["id"])
    assert row["status"] == "denied" and row["decided_by"] is None and row["delivered"] is True
    assert host.held.runners[sid].asked == {}  # запись для перепроверки снята вместе с карточкой


def _core_down_for_auto(host, monkeypatch) -> dict:
    real_post = host.core.post
    state = {"down": True, "asked": 0}

    async def flaky(path, *a, **kw):
        if path.endswith("/auto"):
            state["asked"] += 1
            if state["down"]:
                raise CoreUnavailable("ядро недоступно")
        return await real_post(path, *a, **kw)

    monkeypatch.setattr(host.core, "post", flaky)
    return state


async def test_core_unavailable_no_answer_and_the_pass_is_retried(env, monkeypatch):
    """Ревью, находка 2: ядро не ответило на `/auto` — движку не отвечаем, карточка ждёт, отметок «на
    повтор» нет; следующий круг хоста повторяет проход, и карточка закрывается."""
    env.scenario(turns=[[FOREIGN, {"text": "готово"}]])
    host = await env.started()
    sid, _, _ = await _session(env, mode="auto")
    await _running(env, host, sid)
    card = await _card(env, host, sid)
    state = _core_down_for_auto(host, monkeypatch)
    await _free(env, "foreign_hook")
    await env.until(host, lambda: state["asked"] >= 2, what="проход повторяется кругом хоста")
    assert env.responses() == [] and _row(env, card["id"])["status"] == "pending"
    assert not any(path.endswith("/auto") for path in host.held.unmarked)
    state["down"] = False
    await env.until(host, lambda: env.responses(), what="хост ответил сам")
    await _turn_done(env, host, sid)
    assert [r["response"]["behavior"] for r in env.responses()] == ["allow"]
    row = _row(env, card["id"])
    assert row["status"] == "allowed" and row["decided_by"] == AUTO_BY
    asked = state["asked"]
    await _quiet(env, host)
    assert state["asked"] == asked  # закрыто — повторов больше нет


async def test_switch_returned_while_core_was_down_card_stays_for_owner(env, monkeypatch):
    """Ревью, находка 2: пока ядро не отвечало, галочку вернули — карточка остаётся карточкой, вызов не
    выполнен, решение владельца доставляется как обычно."""
    env.scenario(turns=[[FOREIGN, {"text": "готово"}]])
    host = await env.started()
    sid, _, _ = await _session(env, mode="auto")
    await _running(env, host, sid)
    card = await _card(env, host, sid)
    state = _core_down_for_auto(host, monkeypatch)
    await _free(env, "foreign_hook")
    await env.until(host, lambda: state["asked"] >= 1, what="проход дошёл до ядра")
    await _hold(env, "foreign_hook", True)
    state["down"] = False
    await _quiet(env, host, 5)
    assert env.responses() == [] and _row(env, card["id"])["status"] == "pending"
    assert not any(path.endswith("/auto") for path in host.held.unmarked)
    r = await env.ui("POST", f"/session-requests/{card['id']}/decide", {"decision": "deny"})
    assert r.status_code == 200
    await env.until(host, lambda: env.responses(), what="решение владельца у процесса")
    await _turn_done(env, host, sid)
    assert [r["response"]["behavior"] for r in env.responses()] == ["deny"]
    assert _row(env, card["id"])["delivered"] is True


async def test_asked_does_not_grow(env):
    """Запись запроса для перепроверки живёт, пока ждёт карточка: решена — снята, процесс вышел — снята."""
    env.scenario(turns=[[FOREIGN, FOREIGN, {"sleep": 30}]])
    host = await env.started()
    sid, _, _ = await _session(env, mode="auto")
    await _running(env, host, sid)
    card = await _card(env, host, sid)
    runner = host.held.runners[sid]
    assert len(runner.asked) == 1
    r = await env.ui("POST", f"/session-requests/{card['id']}/decide", {"decision": "allow"})
    assert r.status_code == 200
    await env.until(host, lambda: len(env.responses()) == 1, what="решение доставлено")
    await _card(env, host, sid)  # вторая карточка
    assert len(runner.asked) == 1  # первая запись снята
    await runner.exit_cleanup(runner.process)
    assert runner.asked == {} and runner.pending == {}


async def test_returning_the_switch_and_manual_mode_close_nothing(env):
    env.scenario(turns=[[FOREIGN, {"text": "готово"}]])
    await _free(env, "push")
    host = await env.started()
    sid, _, _ = await _session(env, mode="auto")
    await _running(env, host, sid)
    card = await _card(env, host, sid)
    await _hold(env, "push", True)  # сужение набора
    await _quiet(env, host)
    assert env.responses() == [] and _row(env, card["id"])["status"] == "pending"
    r = await env.ui("POST", f"/session-requests/{card['id']}/decide", {"decision": "allow"})
    assert r.status_code == 200
    await _turn_done(env, host, sid)

    # ручная сессия: выключатели её не касаются
    env.scenario(turns=[[FOREIGN, {"text": "готово"}]])
    manual, _, _ = await _session(env)
    await _running(env, host, manual)
    card = await _card(env, host, manual)
    await _free(env, "foreign_hook", "project_permissions")
    await _quiet(env, host)
    assert await host.held.runners[manual].release_freed() == 0
    assert len(env.responses()) == 1 and _row(env, card["id"])["status"] == "pending"
    r = await env.ui("POST", f"/session-requests/{card['id']}/decide", {"decision": "allow"})
    assert r.status_code == 200
    await _turn_done(env, host, manual)
