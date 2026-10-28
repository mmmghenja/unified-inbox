"""Починки пауз лимита хоста сессий хаба (28.10):

1. Пауза не укорачивается: Codex шлёт отказ с `resetsAt` и следом без него (живой журнал 26.10: пауза до
   13:11, через 2 с — до 09:31); хост берёт дальний срок, `CodexProcess` второй отказ без срока не шлёт.
2. Пауза OpenCode — на провайдера (`opencode:<провайдер>`): отказ ключа anthropic не держит сессии на
   deepseek; старая запись без провайдера читается (хост и ядро); «Снять паузу лимита» снимает и
   составной ключ.
3. После перехода на запасной движок прежняя сессия гасится сразу — без хода в ядро и «HTTP 410».
4. Предупреждение «сохранённые разрешения (always)» — не чаще раза в сутки на папку.

Ядро — в процессе, `claude` — подставной (`fake_claude.py`)."""

import asyncio
import logging
import time

from hub.core import engine_fallback
from hub.engine import codex_process, oc_server

from .test_f83_host_fallback import _paused_with_say, _sessions, codex_home  # noqa: F401
from .test_held_host import _users

FAR = 4_102_444_800  # 2100 год, с


# --- 1. пауза Codex не укорачивается --------------------------------------------------------------


async def test_reset_less_refusal_does_not_shorten_pause(env):
    env.scenario(turns=[[{"text": "ок"}]])
    host = await env.started()
    host.on_rate_limit({"status": "rejected", "engine": "codex", "resetsAt": FAR, "reason": "лимит Codex"})
    first = dict(host.state["pauses"]["codex"])
    assert first["until"] == (FAR + 60) * 1000 and first["kind"] == "limit"
    host.on_rate_limit({"status": "rejected", "engine": "codex", "reason": "лимит Codex"})
    second = host.state["pauses"]["codex"]
    assert second["until"] == first["until"]  # было: now + 30 мин
    assert second["kind"] == "limit" and second["since"] >= first["since"]


async def test_later_reset_extends_pause(env):
    env.scenario(turns=[[{"text": "ок"}]])
    host = await env.started()
    host.on_rate_limit({"status": "rejected", "engine": "codex"})
    short = host.state["pauses"]["codex"]["until"]
    host.on_rate_limit({"status": "rejected", "engine": "codex", "resetsAt": FAR})
    assert host.state["pauses"]["codex"]["until"] == (FAR + 60) * 1000 > short


def _codex_probe() -> tuple[codex_process.CodexProcess, list]:
    proc = codex_process.CodexProcess.__new__(codex_process.CodexProcess)
    sent: list = []
    proc._limit_sent = None  # noqa: SLF001
    proc._limit_reset = None  # noqa: SLF001
    proc._emit = sent.append  # noqa: SLF001
    return proc, sent


def test_codex_skips_reset_less_refusal_while_reset_known():
    proc, sent = _codex_probe()
    reset = time.time() + 3600
    full = {"usedPercent": 100}
    proc._on_limits({"rateLimitReachedType": "x", "primary": {**full, "resetsAt": reset}})  # noqa: SLF001
    proc._on_limits({"rateLimitReachedType": "x", "primary": dict(full)})  # noqa: SLF001
    assert [e["rate_limit_info"].get("resetsAt") for e in sent] == [reset]
    # Срок прошёл — отказ без срока снова новость.
    proc._limit_reset = time.time() - 1  # noqa: SLF001
    proc._on_limits({"rateLimitReachedType": "x", "primary": dict(full)})  # noqa: SLF001
    assert len(sent) == 2 and "resetsAt" not in sent[1]["rate_limit_info"]


# --- 2. пауза OpenCode — на провайдера ------------------------------------------------------------


def _hold(host, model, sid=10**9):
    return host.held._hold({"id": sid, "engine": "opencode", "model": model})  # noqa: SLF001


async def test_provider_pause_holds_only_its_provider(env):
    env.scenario(turns=[[{"text": "ок"}]])
    host = await env.started()
    host.on_rate_limit(
        {"status": "rejected", "engine": "opencode", "provider": "anthropic", "reason": "ключ anthropic"}
    )
    assert set(host.state["pauses"]) == {"opencode:anthropic"}
    assert _hold(host, "anthropic/claude-x") is not None
    assert _hold(host, "deepseek/deepseek-flash") is None
    assert _hold(host, None) is None  # модель по умолчанию — deepseek (`engine.json` нет)
    assert host.held._hold({"engine": "claude"}) is None  # noqa: SLF001
    # В ядро — составным ключом; ядро ищет по модели сессии.
    await env.cycle(host)
    now = host.held.now_ms()
    with env.db.reader() as conn:
        assert engine_fallback.pause_of(conn, "opencode", now, model="anthropic/claude-x") is not None
        assert engine_fallback.pause_of(conn, "opencode", now, model="deepseek/deepseek-flash") is None
        assert engine_fallback.pause_of(conn, "opencode", now) is None


async def test_old_engine_key_still_holds(env):
    env.scenario(turns=[[{"text": "ок"}]])
    host = await env.started()
    now = host.held.now_ms()
    host.state["pauses"] = {"opencode": {"until": now + 600_000, "reason": "старый хост", "since": now}}
    assert _hold(host, "deepseek/deepseek-flash") is not None
    assert _hold(host, "anthropic/claude-x") is not None
    entry = {"until": now + 600_000, "kind": "limit", "since": now}
    env.db.write_sync(lambda conn: engine_fallback.set_host_pauses(conn, {"opencode": entry}))
    with env.db.reader() as conn:
        assert engine_fallback.pause_of(conn, "opencode", now, model="deepseek/deepseek-flash") is not None


def test_parse_host_pauses_keeps_provider_keys():
    raw = (
        '{"opencode:anthropic": {"until": 5, "kind": "auth", "since": 1},'
        ' "nope:x": {"until": 5, "since": 1}, "codex": {"until": 7, "kind": "limit", "since": 1}}'
    )
    assert set(engine_fallback.parse_host_pauses(raw)) == {"opencode:anthropic", "codex"}


async def test_pause_clear_lifts_provider_pause(env):
    env.scenario(turns=[[{"text": "ок"}]])
    host = await env.started()
    host.on_rate_limit({"status": "rejected", "engine": "opencode", "provider": "anthropic"})
    assert host.apply_pause_cleared(host._now_ms() + 1)  # noqa: SLF001
    assert not host.state.get("pauses")


# --- 3. переход на запасной — без хода прежней сессии ---------------------------------------------


async def test_fallback_kills_old_runner_without_410(env, codex_home, caplog):  # noqa: F811
    env.scenario(turns=[[{"text": "ок"}]])
    # Подтверждение живой сессии — каждым кругом: без починки оно уходит прежней сессии в круге перехода.
    host = await env.started(held_confirm_s=0)
    caplog.set_level(logging.WARNING, logger="hub")
    sid, pid = await _paused_with_say(env, host, mode="auto")
    runner = host.held.runners[sid]

    def _switched():
        return any(s.engine == "codex" for s in _sessions(env, pid))

    await env.until(host, _switched, what="переход на запасной")
    assert sid not in host.held.runners and runner.dead  # погашена сразу, не по отказу ядра
    for _ in range(4):
        await env.cycle(host)
    await asyncio.sleep(0.2)
    refused = [r.getMessage() for r in caplog.records if "HTTP 410" in r.getMessage()]
    assert refused == []
    assert len(_users(env)) == 1


# --- 4. предупреждение о сохранённых разрешениях --------------------------------------------------


def test_saved_permissions_warning_once_a_day(monkeypatch):
    monkeypatch.setattr(oc_server, "_saved_warned", {})
    assert oc_server._saved_warn_due("/p", now=1000.0)  # noqa: SLF001
    assert not oc_server._saved_warn_due("/p", now=1000.0 + 3600)  # noqa: SLF001
    assert oc_server._saved_warn_due("/q", now=1000.0 + 3600)  # noqa: SLF001 — другая папка
    assert oc_server._saved_warn_due("/p", now=1000.0 + 86400)  # noqa: SLF001
