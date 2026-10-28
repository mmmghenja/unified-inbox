"""Ф99-7 (docs/architecture.md пп. 1–2, поправки П10 и П11).

7.1 — снятая галочка «Автопилота» закрывает ждущую карточку разрешения и у движков, где решает хост
(OpenCode, Cursor): хост прогоняет своё решение заново, «разрешить» — сначала ядро (`…/auto`), потом ответ
движку; карточку, которую уже решил владелец, хост не трогает.

7.2 — «Всегда в этой сессии»: ставит только кука интерфейса и только на карточке с ключом (точный инструмент
MCP или точный каталог записи вне проекта); действует в этой сессии (и в ручном режиме), снимается в
«Разрешениях», передачей дел не переносится. `opencode`, `cursor-agent` и `claude` — подставные."""

import json
import os
import threading
from pathlib import Path

import pytest

from hub.core import held as core_held
from hub.core import security, session_always
from hub.work import always as always_mod
from hub.work import guard, runner_base

from .oc_helpers import (
    oc,  # noqa: F401 — фикстура `oc`
    started,
)
from .test_f51_release_cards import AUTO_BY, PUSH_MARKED, _row
from .test_held_autopilot import QUESTION, _bash, _free
from .test_held_autopilot_oc import _card
from .test_held_cursor import _cur_session, _pre, cur  # noqa: F401 — фикстура `cur`
from .test_held_cursor import started as cur_started
from .test_held_host import _events, _rec, _running, _session, _turn_done
from .test_held_opencode import MODEL, _oc_session
from .test_held_opencode import _running as _oc_running

SLACK = "mcp__claude_ai_Slack__slack_send_message"


def _not_under_open_roots(path) -> None:
    if str(path).startswith(guard.OPEN_WRITE_ROOTS):
        pytest.skip("временная папка тестов — в /tmp: запись туда гард пропускает и без «всегда»")


async def _decide(env, card_id: int, decision: str = "allow", **extra):
    return await env.ui("POST", f"/session-requests/{card_id}/decide", {"decision": decision, **extra})


# --- 7.1: снятая галочка и карточка движка, где решает хост ------------------------------------------------


async def test_oc_freed_switch_closes_the_waiting_card_core_first(env, oc):  # noqa: F811
    host = await started(env)
    sid, _pid, _workdir = await _oc_session(env, model=MODEL, mode="auto")
    oc.scenario([{"ask": {"tool": "bash", "args": {"command": "git push origin main"}}}, {"text": "готово"}])
    await _oc_running(env, host, oc, sid)
    card = await _card(env, host, sid)
    assert card["rule"] == "push" and oc.replies() == []
    await _free(env, "server")  # чужой ключ карточку не закрывает
    for _ in range(4):
        await env.cycle(host)
    assert oc.replies() == [] and _row(env, card["id"])["status"] == "pending"

    await _free(env, "push")
    await env.until(host, lambda: oc.replies(), what="хост ответил сам")
    await _turn_done(env, host, sid)
    assert [(r["tool"], r["reply"]) for r in oc.replies()] == [("bash", "once")]
    row = _row(env, card["id"])
    assert row["status"] == "allowed" and row["decided_by"] == AUTO_BY and row["delivered"] is True
    assert [e["summary"] for e in await _events(env, sid) if e["kind"] == "decision"] == [
        "галочка снята — разрешено само: Bash"
    ]
    # Повторный прогон — без журнала гарда и без событий «отказ»/«ask».
    log = (env.data_dir / "work" / "guard.log").read_text(encoding="utf-8")
    assert log.count("\task\t") == 1


async def test_oc_card_already_decided_by_owner_is_left_to_her(env, oc, monkeypatch):  # noqa: F811
    """Гонка: пока хост заново решал карточку, владелец отказал — его решение первое, ядро `…/auto` не зовётся,
    движку уходит ровно один ответ (отказ)."""
    entered, release = threading.Event(), threading.Event()
    plain = runner_base.redecide_quietly

    def slow(decide, req, payload):
        entered.set()
        release.wait(10)
        return plain(decide, req, payload)

    monkeypatch.setattr(runner_base, "redecide_quietly", slow)
    host = await started(env)
    sid, _pid, _workdir = await _oc_session(env, model=MODEL, mode="auto")
    oc.scenario([{"ask": {"tool": "bash", "args": {"command": "git push"}}}, {"text": "готово"}])
    await _oc_running(env, host, oc, sid)
    card = await _card(env, host, sid)
    await _free(env, "push")
    await env.until(host, entered.is_set, what="повторное решение началось")
    r = await _decide(env, card["id"], "deny")
    assert r.status_code == 200, r.text
    await env.until(host, lambda: oc.replies(), what="отказ владельца доставлен")
    release.set()
    await _turn_done(env, host, sid)
    for _ in range(3):
        await env.cycle(host)
    assert [r["reply"] for r in oc.replies()] == ["reject"]
    row = _row(env, card["id"])
    assert row["status"] == "denied" and row["decided_by"] is None


async def test_oc_freed_question_is_answered_by_the_session_itself(env, oc):  # noqa: F811
    host = await started(env)
    sid, _pid, _workdir = await _oc_session(env, model=MODEL, mode="auto")
    oc.scenario([{"question": QUESTION}, {"text": "решила"}])
    await _oc_running(env, host, oc, sid)
    card = await _card(env, host, sid)
    assert card["rule"] == "question"
    await _free(env, "question")
    await env.until(host, lambda: oc.replies(), what="хост ответил сам")
    await _turn_done(env, host, sid)
    [answer] = oc.replies()
    assert answer["kind"] == "question" and answer["answers"] == [[held_auto_text()]]
    assert _row(env, card["id"])["decided_by"] == AUTO_BY


def held_auto_text() -> str:
    from hub.work.held import AUTO_ANSWER_TEXT

    return AUTO_ANSWER_TEXT


async def test_cursor_freed_switch_closes_the_waiting_hook_card(env, cur):  # noqa: F811
    host = await cur_started(env)
    sid, _pid, _workdir = await _cur_session(env, mode="auto")
    push = _pre("Shell", {"command": "git push origin main", "cwd": ""})
    cur.scenario(
        [{"tool": "shell", "args": {"command": "git push origin main"}, "hook": push}, {"text": "готово"}]
    )
    await _running(env, host, sid)
    card = await _card(env, host, sid)
    assert card["rule"] == "push"
    await _free(env, "push")
    await _turn_done(env, host, sid)
    assert [h["answer"]["permission"] for h in cur.hooks()] == ["allow"]
    assert _row(env, card["id"])["decided_by"] == AUTO_BY


# --- 7.2: «Всегда в этой сессии» -------------------------------------------------------------------------


def test_card_key_only_for_exact_mcp_tool_and_exact_outside_dir():
    def key(rule, tool, tool_input=None, kind="permission", truncated=False):
        return always_mod.key_of(kind=kind, rule=rule, tool=tool, tool_input=tool_input, truncated=truncated)

    assert key("mcp_write", SLACK) == "mcp:" + SLACK
    assert key("outside_workdir", "Write", {"file_path": "/srv/x/../out/a.txt"}) == "dir:/srv/out"
    assert key("outside_workdir", "Edit", {"file_path": "/srv/out/a.txt"}, truncated=True) is None
    assert key("outside_workdir", "Write", {"file_path": "rel/a.txt"}) is None
    assert key("outside_workdir", "Bash", {"command": "cp a /srv/out/"}) is None  # каталог не точный
    assert key("outside_workdir", "Write", {"file_path": "/a.txt"}) is None
    assert key("mcp_write", "mcp__hub-all__hub_move_channel") is None  # сам хаб
    assert key("mcp_write", "mcp__x__hub_send") is None
    assert key("mcp_write", "WebFetch") is None
    assert key("mcp_write", SLACK, kind="question") is None
    forbidden = (
        "push", "server", "deploy", "kube_write", "helm_write", "gh_write", "foreign_hook",
        "project_permissions", "unparsed_command", "guard_failure", "question", "nested_agent",
    )  # fmt: skip
    for rule in forbidden:
        assert key(rule, SLACK) is None and key(rule, "Write", {"file_path": "/srv/out/a"}) is None


async def test_always_mcp_skips_cards_of_exactly_this_tool_in_manual_mode(env):
    other = "mcp__claude_ai_Slack__slack_upload"
    env.scenario(
        turns=[
            [
                {"ask": {"tool": SLACK, "input": {"text": "раз"}}},
                {"ask": {"tool": SLACK, "input": {"text": "два"}}},
                {"ask": {"tool": SLACK, "input": {"text": "три"}, "reason_type": "hook", "reason": "чужой"}},
                {"ask": {"tool": other, "input": {"text": "x"}}},
                {"text": "готово"},
            ]
        ]
    )
    host = await env.started()
    sid, pid, _workdir = await _session(env)  # ручной режим
    neighbour, _, _ = await _session(env, project=(pid, _workdir), role="сосед")
    await _running(env, host, sid)
    card = await _card(env, host, sid)
    assert card["rule"] == "mcp_write" and card["always"] == "mcp:" + SLACK
    r = await _decide(env, card["id"], always=True)
    assert r.status_code == 200, r.text
    card = await _card(env, host, sid)  # «раз» разрешён, «два» прошёл сам; «три» спросил чужой хук
    assert card["tool"] == SLACK and card["input"] == {"text": "три"}
    assert (await _decide(env, card["id"], "deny")).status_code == 200
    card = await _card(env, host, sid)
    assert card["tool"] == other and card["always"] == "mcp:" + other
    assert (await _decide(env, card["id"], "deny")).status_code == 200
    await _turn_done(env, host, sid)
    assert [r["response"]["behavior"] for r in env.responses()] == ["allow", "allow", "deny", "deny"]
    assert [i["key"] for i in (await _rec(env, sid))["always"]] == ["mcp:" + SLACK]
    assert (await _rec(env, neighbour))["always"] == []  # соседняя сессия — без «всегда»
    log = (env.data_dir / "work" / "guard.log").read_text(encoding="utf-8")
    assert log.count("\talways\tвсегда в этой сессии") == 1
    assert "always" not in str([r["response"] for r in env.responses()])  # движку — разовое «разрешить»


async def test_always_dir_lets_the_guard_skip_exactly_that_dir_in_this_session_only(env, oc, tmp_path):  # noqa: F811
    _not_under_open_roots(tmp_path)
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    host = await started(env)
    sid, pid, workdir = await _oc_session(env, model=MODEL)
    neighbour, _, _ = await _oc_session(env, model=MODEL, project=(pid, workdir), role="сосед")

    def write(path):
        return {"ask": {"tool": "write", "args": {"filePath": str(path), "content": "x"}}}

    oc.scenario(
        [write(outside / "a.txt"), write(outside / "b.txt"), write(outside / "sub" / "c.txt"), {"text": "ок"}]
    )
    await _oc_running(env, host, oc, sid)
    card = await _card(env, host, sid)
    assert card["rule"] == "outside_workdir" and card["always"] == f"dir:{outside}"
    assert (await _decide(env, card["id"], always=True)).status_code == 200
    card = await _card(env, host, sid)  # b.txt прошёл сам, подкаталог — карточка
    assert card["input"]["file_path"] == str(outside / "sub" / "c.txt")
    assert (await _decide(env, card["id"], "deny")).status_code == 200
    await _turn_done(env, host, sid)
    assert [r["reply"] for r in oc.replies()] == ["once", "once", "reject"]
    assert host.held.runners[sid].always == {f"dir:{outside}"}
    assert (await _rec(env, neighbour))["always"] == []
    oc.scenario([write(outside / "d.txt"), {"text": "ок"}])
    await _oc_running(env, host, oc, neighbour)
    card = await _card(env, host, neighbour)
    assert card["rule"] == "outside_workdir"
    assert (await _decide(env, card["id"], "deny")).status_code == 200
    await _turn_done(env, host, neighbour)


def test_guard_decide_and_free_sock_carry_always_dirs(tmp_path):
    _not_under_open_roots(tmp_path)
    workdir, home = tmp_path / "proj", tmp_path / "home"
    out = tmp_path / "out"
    for d in (workdir, home, out / "sub"):
        d.mkdir(parents=True)
    event = {"tool_name": "Write", "tool_input": {"file_path": str(out / "a.txt"), "content": "x"}}
    kw = {"workdir": str(workdir), "home": str(home)}
    try:
        guard.decide(event, **kw)
        raise AssertionError("без «всегда» — карточка")
    except guard.Verdict as verdict:
        assert verdict.key == "outside_workdir"
    guard.decide(event, free={f"dir:{out}"}, **kw)  # пропуск
    sub = {"tool_name": "Write", "tool_input": {"file_path": str(out / "sub" / "b.txt")}}
    try:
        guard.decide(sub, free={f"dir:{out}"}, **kw)
        raise AssertionError("подкаталог — карточка")
    except guard.Verdict as verdict:
        assert verdict.key == "outside_workdir"
    # Ревью Ф99: Bash в тот же каталог «всегда» не открывает — только инструмент файлов (П11).
    try:
        bash = {"tool_name": "Bash", "tool_input": {"command": f"echo x > {out}/c.txt"}}
        guard.decide(bash, free={f"dir:{out}"}, **kw)
        raise AssertionError("Bash — карточка")
    except guard.Verdict as verdict:
        assert verdict.key == "outside_workdir"

    from hub.work import free_sock

    server = free_sock.FreeServer(lambda sid: ["push", f"dir:{out}"] if sid == 7 else [])
    server.start()
    try:
        assert guard.read_free_sock(server.name, os.getpid(), 7) == ({"push", f"dir:{out}"}, None)
        assert guard.read_free_sock(server.name, os.getpid(), 8) == (frozenset(), None)
    finally:
        server.close()
    bad = free_sock.FreeServer(lambda sid: ["dir:relative"])
    bad.start()
    try:
        assert guard.read_free_sock(bad.name, os.getpid(), 1)[1] == "ответ хоста неразборчив"
    finally:
        bad.close()


async def test_core_refuses_always_for_forbidden_cards_and_for_non_cookie(env):
    env.scenario(
        turns=[
            [
                _bash("git push", reason_type="hook", reason=PUSH_MARKED),
                {"ask": {"tool": "AskUserQuestion", "input": QUESTION}},
                {"ask": {"tool": SLACK, "input": {"text": "x"}}},
                {"text": "готово"},
            ]
        ]
    )
    host = await env.started()
    sid, _pid, _workdir = await _session(env)
    await _running(env, host, sid)
    card = await _card(env, host, sid)
    assert card["rule"] == "push" and card["always"] is None
    r = await _decide(env, card["id"], always=True)
    assert r.status_code == 422 and "нет" in r.text
    assert (await _decide(env, card["id"], "deny")).status_code == 200
    card = await _card(env, host, sid)
    assert card["kind"] == "question" and card["always"] is None
    r = await _decide(env, card["id"], always=True, answers={"Какой стенд брать?": "dev"})
    assert r.status_code == 422
    assert (await _decide(env, card["id"], "deny")).status_code == 200
    card = await _card(env, host, sid)
    assert card["always"] == "mcp:" + SLACK
    r = await _decide(env, card["id"], "deny", always=True)  # только с «разрешить»
    assert r.status_code == 422
    # Не кука: агент владельца (скоуп `human`) и прочие bearer — 403 на ручке решения.
    created = env.db.write_sync(
        lambda conn: security.create_api_token(
            conn, name="owner-agent", kind="background", scopes=["human"], project_ids=None, expires_ts=None
        )
    )
    r = await env.client.post(
        f"/api/v1/session-requests/{card['id']}/decide",
        json={"decision": "allow", "always": True},
        headers={"Authorization": f"Bearer {created.token}"},
    )
    assert r.status_code == 403
    r = await env.client.post(
        f"/api/v1/sessions/{sid}/always/remove",
        json={"key": "mcp:" + SLACK},
        headers={"Authorization": f"Bearer {created.token}"},
    )
    assert r.status_code == 403
    assert (await _decide(env, card["id"], "deny")).status_code == 200
    await _turn_done(env, host, sid)
    assert (await _rec(env, sid))["always"] == []


async def test_always_is_removed_by_cookie_and_not_carried_by_handoff(env):
    sid, _pid, _workdir = await _session(env)
    key = "mcp:" + SLACK

    def put(conn):
        session_always.add(conn, core_held.get(conn, sid), key, now_ms=1)
        session_always.add(conn, core_held.get(conn, sid), "dir:/srv/out", now_ms=2)

    env.db.write_sync(put)
    rec = await _rec(env, sid)
    assert [i["key"] for i in rec["always"]] == [key, "dir:/srv/out"]
    r = await env.ui("POST", f"/sessions/{sid}/always/remove", {"key": "dir:/srv/out"})
    assert r.status_code == 200, r.text
    assert [i["key"] for i in r.json()["session"]["always"]] == [key]
    r = await env.ui("POST", f"/sessions/{sid}/always/remove", {"key": "dir:/srv/out"})
    assert r.status_code == 404
    r = await env.ui("POST", f"/sessions/{sid}/handoff", {})
    assert r.status_code == 200, r.text
    assert r.json()["session"]["always"] == [] and r.json()["closed"]["always"] == [{"key": key, "ts": 1}]


async def test_host_applies_always_in_place(env):
    """Набор «всегда» хост берёт из записи сессии при каждом обновлении — без перезапуска процесса."""
    env.scenario(turns=[[{"text": "раз"}], [{"text": "два"}]])
    host = await env.started()
    sid, _pid, _workdir = await _session(env)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    runner = host.held.runners[sid]
    assert runner.always == frozenset()
    env.db.write_sync(
        lambda conn: session_always.add(conn, core_held.get(conn, sid), "dir:/srv/out", now_ms=5)
    )
    await env.until(host, lambda: runner.always == {"dir:/srv/out"}, what="набор на месте")
    assert "dir:/srv/out" in host.held.free_keys(sid)
    assert len(env.calls()) == 1


def test_review_always_dir_home_and_dotfiles_and_bash(tmp_path, monkeypatch):
    """Ревью Ф99 (блокер): «всегда» на файл в домашнем каталоге не даётся; ключ каталога действует только для
    инструмента файлов и не на файлы с именем на «.»; цели Bash идут карточкой как раньше."""
    from hub.work import always, guard

    def verdict(event, **kw):
        try:
            guard.decide(event, **kw)
        except guard.Verdict as v:
            return v
        return None

    home = tmp_path / "home"
    work = home / "projects" / "p"
    work.mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Ключа на сам домашний каталог и его предков нет; на файл с именем на «.» — тоже.
    for target in (home / "notes.md", tmp_path / "x.md", other / ".rc"):
        assert always.key_of(
            kind="permission", rule="outside_workdir", tool="Write", tool_input={"file_path": str(target)}
        ) is None
    key = always.key_of(
        kind="permission", rule="outside_workdir", tool="Write", tool_input={"file_path": str(other / "a.md")}
    )
    assert key == "dir:" + str(other)
    base = dict(workdir=str(work), home=str(home), cwd_unknown=False, free=frozenset({key}))

    def write(path):
        tool_input = {"file_path": str(path), "content": "x"}
        return {"tool_name": "Write", "tool_input": tool_input, "cwd": str(work)}

    assert verdict(write(other / "b.md"), **base) is None  # инструмент файлов, тот же каталог — без карточки
    v = verdict(write(other / ".rc"), **base)
    assert v is not None and v.key == "outside_workdir"
    bash = {"tool_name": "Bash", "tool_input": {"command": f"echo x >> {other / 'b.md'}"}, "cwd": str(work)}
    v = verdict(bash, **base)
    assert v is not None and v.key == "outside_workdir"
    # Старый набор с ключом на домашний каталог (до правки ядра) гарду ничего не открывает.
    stale = dict(base, free=frozenset({"dir:" + str(home)}))
    v = verdict(write(home / "notes.md"), **stale)
    assert v is not None and v.key == "outside_workdir"


async def test_review_always_mcp_not_over_project_hook_on_this_tool(env):
    """Ревью Ф99: на инструмент висит PreToolUse-хук проекта — тип запроса `rule` мог прийти и от него, и
    «всегда» такой запрос не закрывает (карточка владельцу)."""
    env.scenario(
        turns=[
            [
                {"ask": {"tool": SLACK, "input": {"text": "раз"}}},
                {"ask": {"tool": SLACK, "input": {"text": "два"}}},
                {"text": "готово"},
            ]
        ]
    )
    host = await env.started()
    sid, _pid, workdir = await _session(env)
    settings = Path(workdir) / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    hook = {"type": "command", "command": "true"}
    entry = {"matcher": "mcp__.*slack.*", "hooks": [hook]}
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [entry]}}))
    await _running(env, host, sid)
    card = await _card(env, host, sid)
    assert (await _decide(env, card["id"], always=True)).status_code == 200
    card = await _card(env, host, sid)  # «два» — снова карточка: хук проекта на этом инструменте
    assert card["tool"] == SLACK and card["input"] == {"text": "два"}
    assert (await _decide(env, card["id"], "deny")).status_code == 200
    await _turn_done(env, host, sid)
