"""Ф99-1 (docs/architecture.md, поправки П1, П2): сессия хаба из прошлого разговора Claude Code.
Хост идёт `--resume`; первая подача несёт ВЕСЬ системный текст хаба первым блоком и после неё ядру пишется
снимок `sys_pin` с постоянным признаком `imported`; дальше — обычная поправка Ф48-5, а после каждого сжатия —
снова весь текст. Проверка `claude agents --json` у
такой сессии до первого снимка обязательна: сбой — отказ запуска с причиной. Движок поддельный."""

import json
import uuid

import pytest

from hub.core import subs
from hub.work import held as held_mod
from hub.work import session

from .conftest import flag_value
from .test_f48_5_pinned_text import _fixes, _pin, _resumed_turn, _sleep
from .test_held_compact import _compacted, _compacts, _use
from .test_held_host import _rec, _say, _turn_done, _users

HEAD = held_mod.PIN_IMPORT_HEAD


@pytest.fixture
def claude_home(env, monkeypatch):
    """Ядро ищет разговоры в каталоге Claude той же машины — в тестах это `home/.claude` хоста."""
    root = env.home / ".claude"
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    return root


def _conv(root, workdir) -> str:
    conv_id = str(uuid.uuid4())
    folder = root / "projects" / "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in str(workdir))
    folder.mkdir(parents=True, exist_ok=True)
    record = {
        "type": "user",
        "message": {"role": "user", "content": "про дачу"},
        "timestamp": "2026-10-27T10:00:00Z",
    }
    (folder / f"{conv_id}.jsonl").write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    return conv_id


async def _imported(env, claude_home, **body):
    pid, workdir = env.project("Личное")
    env.db.write_sync(lambda conn: subs.set_project_subs(conn, pid, True, now_ms=0))
    conv = _conv(claude_home, workdir)
    r = await env.ui(
        "POST", "/sessions", {"project_id": pid, "engine": "claude", "resume_conv": conv, **body}
    )
    assert r.status_code == 200, r.text
    return r.json()["session"]["id"], conv, workdir


async def test_first_feed_carries_whole_system_text_then_snapshot(env, claude_home):
    env.scenario(turns=[[{"text": "продолжаю"}]])
    host = await env.started()
    sid, conv, _ = await _imported(env, claude_home, role="тестер")
    assert _pin(env, sid) == {"imported": True}
    say_id = await _say(env, sid, "давай дальше")
    await env.until(host, lambda: len(_users(env)) == 1, what="реплика подана")
    await _turn_done(env, host, sid)
    argv = env.calls()[0]["argv"]
    assert flag_value(argv, "--resume") == conv and "--session-id" not in argv
    system = flag_value(argv, "--append-system-prompt")
    blocks = _users(env)[0]["message"]["content"]
    # Весь системный текст — первым блоком, реплика владельца — следом, как есть.
    assert blocks[0]["text"] == f"{HEAD}\n\n{system}"
    assert blocks[-1]["text"] == "давай дальше"
    assert _users(env)[0]["uuid"] == session.held_say_uuid(say_id)
    # Снимок — как у нового разговора, плюс постоянный признак «импортирован».
    pin = _pin(env, sid)
    assert pin is not None and pin["imported"] is True and "mode" in pin
    assert pin["role"] == "тестер" and pin["mode"] == "manual"
    rec = await _rec(env, sid)
    assert rec["engine_session_id"] == conv and rec["state"] == "idle"
    # Следующий запуск — обычный Ф48-5: отличий нет, вход прежний байт в байт.
    await _sleep(env, host, sid)
    env.scenario(turns=[[{"text": "второй"}]])
    line = await _resumed_turn(env, host, sid, "ещё")
    assert "--resume" in env.calls()[-1]["argv"]
    assert [b["text"] for b in line["message"]["content"]] == ["ещё"]
    assert len(_fixes(env)) == 0  # поправка Ф48-5 не шла; полный текст — только у первой подачи


async def test_agents_check_failure_refuses_launch(env, claude_home):
    host = await env.started()
    sid, _, _ = await _imported(env, claude_home)
    (env.bin_dir / "agents.json").write_text(json.dumps({"raw": "не json", "rc": 0}))
    await _say(env, sid, "давай дальше")

    async def failed():
        return (await _rec(env, sid))["failure"]

    failure = await env.until(host, failed, what="причина у сессии")
    assert "не проверить, не открыт ли разговор в другом окне" in failure
    assert env.calls() == []  # процесс не запускался
    assert _pin(env, sid) == {"imported": True}


async def test_agents_check_timeout_refuses_launch(env, claude_home, monkeypatch):
    monkeypatch.setattr(held_mod, "AGENTS_TIMEOUT_S", 0.3)
    host = await env.started()
    sid, _, _ = await _imported(env, claude_home)
    (env.bin_dir / "agents.json").write_text(json.dumps({"sleep": 3, "raw": "[]"}))
    await _say(env, sid, "давай дальше")

    async def failed():
        return (await _rec(env, sid))["failure"]

    failure = await env.until(host, failed, what="причина у сессии")
    assert "не ответила вовремя" in failure and env.calls() == []


async def test_missing_transcript_refuses_launch(env, claude_home):
    host = await env.started()
    sid, conv, workdir = await _imported(env, claude_home)
    for path in (claude_home / "projects").glob(f"*/{conv}.jsonl"):
        path.unlink()
    await _say(env, sid, "давай дальше")

    async def failed():
        return (await _rec(env, sid))["failure"]

    failure = await env.until(host, failed, what="причина у сессии")
    assert "прошлого разговора Claude Code больше нет" in failure and env.calls() == []


async def test_imported_flag_lives_on_and_full_text_follows_every_compaction(env, claude_home):
    """Признак «импортирован» в снимке постоянен: второй запуск без поправки, но после сжатия хоста и после
    сжатия самого движка первой подачей снова идёт весь системный текст хаба."""
    env.scenario(turns=[[{"text": "раз"}, _use(20_000)]], init_model="claude-test")
    host = await env.started(held_compact_min_growth=0, held_compact_tokens=150_000)
    sid, _, _ = await _imported(env, claude_home, role="тестер")
    await _say(env, sid, "давай дальше")
    await env.until(host, lambda: len(_users(env)) == 1, what="первая подача")
    await _turn_done(env, host, sid)
    await env.until(host, lambda: "mode" in (_pin(env, sid) or {}), what="снимок записан")
    assert _pin(env, sid)["imported"] is True
    await _sleep(env, host, sid)

    env.scenario(
        turns=[
            [{"text": "два"}, _use(30_000)],
            [{"text": "три"}, _use(160_000)],
            [{"text": "четыре"}],
            [{"text": "пять"}],
        ],
        compact={"pre_tokens": 161_000},
        init_model="claude-test",
    )
    line = await _resumed_turn(env, host, sid, "ещё")
    assert [b["text"] for b in line["message"]["content"]] == ["ещё"]  # второй запуск: отличий нет
    full = f"{HEAD}\n\n{flag_value(env.calls()[-1]['argv'], '--append-system-prompt')}"
    assert host.held.runners[sid].pin_full == full

    await _say(env, sid, "дальше")
    await env.until(host, lambda: len(_users(env)) == 3, what="вторая реплика")
    await _compacted(env, host, sid)
    assert _compacts(env) == 1
    await _say(env, sid, "после сжатия")
    await env.until(host, lambda: len(_users(env)) == 5, what="реплика после сжатия")
    await _turn_done(env, host, sid)
    assert [b["text"] for b in _users(env)[-1]["message"]["content"]] == [full, "после сжатия"]

    # Сжатие самим движком (граница вне сжатия хоста) — тоже весь текст первой подачей.
    await host.held.runners[sid].on_message({"type": "system", "subtype": "compact_boundary"})
    await _say(env, sid, "снова")
    await env.until(host, lambda: len(_users(env)) == 6, what="реплика после сжатия движка")
    await _turn_done(env, host, sid)
    assert [b["text"] for b in _users(env)[-1]["message"]["content"]] == [full, "снова"]
    assert _pin(env, sid)["imported"] is True  # признак пережил все привязки


async def test_regular_session_has_no_full_text(env):
    env.scenario(turns=[[{"text": "раз"}]])
    host = await env.started()
    pid, _ = env.project("Initech")
    env.db.write_sync(lambda conn: subs.set_project_subs(conn, pid, True, now_ms=0))
    r = await env.ui("POST", "/sessions", {"project_id": pid, "engine": "claude"})
    sid = r.json()["session"]["id"]
    await _say(env, sid, "привет")
    await env.until(host, lambda: len(_users(env)) == 1, what="подача")
    await _turn_done(env, host, sid)
    assert host.held.runners[sid].pin_full is None and "imported" not in _pin(env, sid)
