"""Ф78-2 П1 (docs/architecture.md): хост `hub-work` принимает любой движок
переключателя. Прежний фильтр «claude/opencode» оставлял хосту старый движок при `codex`/`cursor`, а ядро на
несовпадение отвечало на `/work/host/wait` сразу — горячая петля и запись `state.json` на каждом круге."""

import time

from hub.core import engine as engine_mod


def _set_engine(env, engine: str) -> None:
    env.db.write_sync(lambda conn: engine_mod._meta_set(conn, engine_mod.META_ENGINE, engine))  # noqa: SLF001


async def _wait_s(host, timeout_s: float) -> tuple[float, dict]:
    started = time.monotonic()
    data = await host.core.get("/work/host/wait", params=host.wait_params(timeout_s))
    return time.monotonic() - started, data


async def test_host_on_codex_waits_instead_of_spinning(env):
    _set_engine(env, "codex")
    host = await env.started()
    assert host.engine == "codex"  # снимок `/work/host/open`
    took, _data = await _wait_s(host, 0.6)
    assert took >= 0.5, f"ожидание вернулось за {took:.2f} с — горячая петля"


async def test_host_follows_switch_to_cursor_then_waits(env):
    host = await env.started()
    assert host.engine == "claude"
    _set_engine(env, "cursor")
    took, data = await _wait_s(host, 0.6)
    assert took < 0.5 and data["engine"] == "cursor"  # смена движка будит ожидание сразу
    await host.apply(data)
    assert host.engine == "cursor"
    took, _data = await _wait_s(host, 0.6)
    assert took >= 0.5, f"ожидание вернулось за {took:.2f} с — горячая петля"
