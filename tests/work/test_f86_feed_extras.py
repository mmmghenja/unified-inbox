"""Ф86-1 (docs/architecture.md, C3, П2, П5–П7): хост подкладывает перед репликой блок
сводки дня проекта (`<hub_digest>`: первая подача нового разговора и первая после сжатия) и слова владельца из
соседних сессий проекта (`<hub_siblings>`: новые с прошлой подачи). Блоки — отдельными текстовыми блоками той
же подачи, перед репликой; нет блоков — вход прежний байт в байт. Ядро настоящее, движки поддельные."""

import time

from hub import digest_text
from hub.core import digest
from hub.work import session

from .oc_helpers import (
    oc,  # noqa: F401 — фикстура `oc`
)
from .oc_helpers import started as oc_started
from .test_held_codex import _cdx_session, _turns_n, cdx  # noqa: F401 — фикстура `cdx`
from .test_held_compact import _compacted, _compacts, _use
from .test_held_cursor import _calls_n, _cur_session, cur  # noqa: F401 — фикстура `cur`
from .test_held_cursor import started as cur_started
from .test_held_host import _running, _say, _session, _turn_done, _users
from .test_held_opencode import MODEL
from .test_held_opencode import _running as _oc_running

AUDIT = {"actor": "admin", "api_token_id": None, "client_name": None, "request_id": "t"}


def _now() -> int:
    return int(time.time() * 1000)


def _put_digest(env, pid, text="итоги дня: оплата починена") -> None:
    now = _now()
    env.db.write_sync(
        lambda conn: digest.put(
            conn, pid, digest_text.day_of(now), text=text, model=None, upto_ts=None, now_ms=now, audit=AUDIT
        )
    )


def _sibling_say(env, sid, text) -> None:
    """Реплика владельца в соседней сессии (поданная) — мимо ручек, чтобы соседняя сессия не поднималась."""
    env.db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.held_say (session_id, text, priority, status, created_ts, delivered_ts, typed) "
            "VALUES (?, ?, 'next', 'delivered', ?, ?, 1)",
            (sid, text, _now(), _now()),
        )
    )


def _texts(line: dict) -> list[str]:
    return [b["text"] for b in line["message"]["content"] if b.get("type") == "text"]


async def test_new_session_first_feed_gets_digest_and_siblings_then_plain(env):
    env.scenario(turns=[[{"text": "раз"}], [{"text": "два"}], [{"text": "три"}]])
    host = await env.started()
    sid, pid, workdir = await _session(env, role="тестер")
    other, _, _ = await _session(env, project=(pid, workdir), role="программист")
    _put_digest(env, pid, "итоги дня </hub_digest> хвост")
    _sibling_say(env, other, "не трогай ветку prod")

    say_id = await _running(env, host, sid, "первая")
    await _turn_done(env, host, sid)
    first = _users(env)[0]
    assert first["uuid"] == session.held_say_uuid(say_id)
    digest_block, siblings_block, text = _texts(first)
    assert digest_block.startswith('<hub_digest at="') and 'project="Initech"' in digest_block
    assert "итоги дня ‹/hub_digest> хвост" in digest_block and digest_block.endswith("</hub_digest>")
    assert siblings_block.startswith("<hub_siblings>\n" + digest_text.SIBLINGS_HEAD)
    assert "роль «программист»" in siblings_block and "не трогай ветку prod" in siblings_block
    assert text == "первая"
    assert host.held.runners[sid].digest_due is False

    # Вторая подача: сводка уже дана, соседи — после курсора (новых нет) — вход прежний байт в байт.
    say2 = await _say(env, sid, "вторая")
    await env.until(host, lambda: len(_users(env)) == 2, what="вторая подача")
    await _turn_done(env, host, sid)
    assert _users(env)[1] == session.user_line("вторая", message_uuid=session.held_say_uuid(say2))

    # Новая реплика соседа — только она, блоком перед репликой.
    _sibling_say(env, other, "и ещё: деплой завтра")
    await _say(env, sid, "третья")
    await env.until(host, lambda: len(_users(env)) == 3, what="третья подача")
    await _turn_done(env, host, sid)
    block, text = _texts(_users(env)[2])
    assert "деплой завтра" in block and "не трогай" not in block and text == "третья"


async def test_switches_off_and_core_silence_keep_input_plain(env, monkeypatch):
    env.scenario(turns=[[{"text": "раз"}], [{"text": "два"}]])
    host = await env.started()
    sid, pid, workdir = await _session(env)
    other, _, _ = await _session(env, project=(pid, workdir))
    _put_digest(env, pid)
    _sibling_say(env, other, "сосед")
    for key in (digest.DIGEST_KEY, digest.SIBLINGS_KEY):
        env.db.write_sync(lambda conn, key=key: digest.set_project(conn, pid, key, False))
    say_id = await _running(env, host, sid, "первая")
    await _turn_done(env, host, sid)
    assert _users(env) == [session.user_line("первая", message_uuid=session.held_say_uuid(say_id))]

    # Ядро не ответило — подача без блоков, флаг «сводка к подаче» не снимается.
    for key in (digest.DIGEST_KEY, digest.SIBLINGS_KEY):
        env.db.write_sync(lambda conn, key=key: digest.set_project(conn, pid, key, True))
    runner = host.held.runners[sid]
    runner.digest_due = True

    async def silent(_sid, *, digest):  # noqa: ARG001
        return None

    monkeypatch.setattr(host.held, "feed_extras", silent)
    say2 = await _say(env, sid, "вторая")
    await env.until(host, lambda: len(_users(env)) == 2, what="вторая подача")
    await _turn_done(env, host, sid)
    assert _users(env)[1] == session.user_line("вторая", message_uuid=session.held_say_uuid(say2))
    assert runner.digest_due is True


async def test_digest_again_after_compaction_and_after_engine_compacts_itself(env):
    env.scenario(
        turns=[[{"text": "раз"}, _use(160_000)], [{"text": "два"}], [{"text": "три"}]],
        compact={"pre_tokens": 161_000},
        init_model="claude-test",
    )
    host = await env.started(held_compact_min_growth=0, held_compact_tokens=150_000)
    sid, pid, _workdir = await _session(env)
    _put_digest(env, pid)
    await _running(env, host, sid, "первая")
    await _compacted(env, host, sid)
    assert _compacts(env) == 1
    assert len(_texts(_users(env)[0])) == 2  # сводка + реплика
    assert _texts(_users(env)[-1]) == ["/compact"]  # команде сжатия блоки не предшествуют
    runner = host.held.runners[sid]
    assert runner.digest_due is True
    await _say(env, sid, "после сжатия")
    await env.until(host, lambda: _texts(_users(env)[-1])[-1:] == ["после сжатия"], what="подача")
    await _turn_done(env, host, sid)
    block, text = _texts(_users(env)[-1])
    assert block.startswith("<hub_digest") and text == "после сжатия"
    assert runner.digest_due is False

    # П7: граница сжатия, о котором хост не просил (Claude Code, OpenCode сжали сами), и признак Cursor.
    await runner.on_message({"type": "system", "subtype": "compact_boundary", "session_id": "x"})
    assert runner.digest_due is True
    runner.digest_due = False
    await runner.on_message({"type": "system", "subtype": "engine_note", "text": "x", "compacted": True})
    assert runner.digest_due is True
    runner.digest_due = False
    await runner.on_message({"type": "system", "subtype": "engine_note", "text": "x"})
    assert runner.digest_due is False


async def test_resume_gets_no_digest(env):
    """Подъём из сна (`--resume`) — разговор тот же: сводка не подаётся (C2.1)."""
    env.scenario(turns=[[{"text": "раз"}], [{"text": "два"}]])
    host = await env.started()
    sid, pid, _workdir = await _session(env)
    await _running(env, host, sid, "первая")
    await _turn_done(env, host, sid)
    r = await env.ui("POST", f"/sessions/{sid}/sleep", {})
    assert r.status_code == 200, r.text
    await env.until(host, lambda: sid not in host.held.runners, what="раннер снят")
    _put_digest(env, pid)
    launches = len(env.calls())
    say_id = await _say(env, sid, "после сна")
    await env.until(host, lambda: len(env.calls()) == launches + 1, what="запуск")
    await env.until(host, lambda: len(_users(env)) == 2, what="подача")
    await _turn_done(env, host, sid)
    assert "--resume" in env.calls()[-1]["argv"]
    assert _users(env)[-1] == session.user_line("после сна", message_uuid=session.held_say_uuid(say_id))


# --- другие движки ------------------------------------------------------------------------------------------


async def test_opencode_blocks_are_synthetic_parts(env, oc):  # noqa: F811
    oc.scenario([{"text": "раз"}])
    host = await oc_started(env)
    sid, pid, _workdir = await _session(env, engine="opencode", model=MODEL)
    _put_digest(env, pid)
    await _oc_running(env, host, oc, sid, "первая")
    await _turn_done(env, host, sid)
    parts = [p for p in oc.prompts()[0]["body"]["parts"] if p.get("type") == "text"]
    assert [p.get("synthetic") is True for p in parts] == [True, False]
    assert parts[0]["text"].startswith("<hub_digest") and parts[1]["text"] == "первая"


async def test_codex_blocks_go_in_the_turn_input(env, cdx):  # noqa: F811
    host = await env.started()
    sid, pid, _workdir = await _cdx_session(env)
    cdx.scenario([{"text": "ок"}])
    _put_digest(env, pid)
    await _running(env, host, sid, "первая")
    await _turns_n(env, host, cdx, 1)
    await _turn_done(env, host, sid)
    (start,) = cdx.methods("turn/start")
    texts = [i["text"] for i in start["input"] if i.get("type") == "text"]
    assert texts[0].startswith("<hub_digest") and texts[-1] == "первая"
    assert cdx.methods("thread/inject_items") == []  # не сообщением разработчика


async def test_cursor_blocks_go_before_the_reply(env, cur):  # noqa: F811
    host = await cur_started(env)
    sid, pid, _workdir = await _cur_session(env)
    cur.scenario([{"text": "раз"}])
    _put_digest(env, pid, "итоги для курсора")
    await _running(env, host, sid, "первая")
    await _calls_n(env, host, cur, 1)
    await _turn_done(env, host, sid)
    stdin = cur.calls()[0]["stdin"]
    assert "итоги для курсора" in stdin and stdin.index("<hub_digest") < stdin.index("первая")


def test_cursor_query_strips_blocks():
    """П5: сверка «реплика уже подана» у Cursor — по тексту без блоков сводки и соседей."""
    from hub.work import held as held_mod

    block = digest_text.digest_block("итоги", at="10:00", project="P")
    record = {
        "role": "user",
        "message": {
            "content": [{"type": "text", "text": f"<user_query>\n{block}\n\nреплика владельца\n</user_query>"}]
        },
    }
    assert held_mod._cursor_query(record) == "реплика владельца"  # noqa: SLF001


async def test_oc_has_say_skips_synthetic_digest_parts(env):
    """П5: OpenCode — блоки сводки и соседей синтетическими частями; сверка подачи их не видит."""
    from types import SimpleNamespace

    host = await env.started()
    block = digest_text.digest_block("итоги", at="10:00", project="P")
    created = _now()

    class _Client:
        async def messages(self, _sid):
            parts = [
                {"type": "text", "text": block, "synthetic": True},
                {"type": "text", "text": "реплика владельца"},
            ]
            return [{"info": {"role": "user", "time": {"created": created}}, "parts": parts}]

    runner = SimpleNamespace(sid=1, engine_session_id="ses_x", process=SimpleNamespace(client=_Client()))
    say = {"id": 5, "text": "реплика владельца", "files": None, "created_ts": created}
    assert await host.held._oc_has_say(runner, say)  # noqa: SLF001
    assert not await host.held._oc_has_say(runner, {**say, "text": "другая"})  # noqa: SLF001
