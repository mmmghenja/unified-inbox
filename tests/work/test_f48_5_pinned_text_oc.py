"""Ф48-5 (docs/architecture.md): OpenCode поправок не получает и снимка не пишет — у
него системный текст идёт с каждым ходом. `opencode` — поддельный сервер, как в test_held_opencode.py."""

import json

from hub.work import held as held_mod

from .oc_helpers import (
    oc,  # noqa: F401 — фикстура `oc`
    started,
)
from .test_f48_5_pinned_text import BODY, PATH, _pin, _write
from .test_held_host import _rec, _say, _session, _turn_done
from .test_held_opencode import MODEL, _running


async def test_oc_gets_no_snapshot_and_no_fix(env, oc):  # noqa: F811
    oc.scenario([{"text": "раз"}])
    host = await started(env)
    pid, workdir = env.project("Initech")
    _write(workdir, BODY)
    sid, _, _ = await _session(
        env, project=(pid, workdir), engine="opencode", model=MODEL, role="тестер", role_file=PATH
    )
    await _running(env, host, oc, sid)
    await _turn_done(env, host, sid)
    assert _pin(env, sid) is None and "sys_pin" not in await _rec(env, sid)
    await env.ui("POST", f"/sessions/{sid}/sleep")
    await env.until(
        host, lambda: sid not in host.held.runners and not oc.server_alive(), what="сервер погашен"
    )
    # Всё, что у Claude Code дало бы поправку: файл, режим.
    new_body = "вторая редакция\n"
    _write(workdir, new_body)
    r = await env.ui("PATCH", f"/sessions/{sid}", {"mode": "auto"})
    assert r.status_code == 200, r.text
    oc.scenario([{"text": "два"}])
    await _say(env, sid, "ещё")
    await env.until(host, lambda: len(oc.prompts()) == 2, what="второй ход")
    await _turn_done(env, host, sid)
    runner = host.held.runners[sid]
    assert runner.pin_fix is None and runner.pin_fix_due is False
    second = oc.prompts()[1]["body"]
    assert held_mod.PIN_FIX_HEAD not in json.dumps(second["parts"], ensure_ascii=False)
    assert [p["text"] for p in second["parts"] if p.get("type") == "text"] == ["ещё"]
    assert new_body in second["system"]  # новая редакция — в системном тексте хода
    assert _pin(env, sid) is None
