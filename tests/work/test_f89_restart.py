"""Ф89-1 (docs/architecture.md и поправка П12): строка `<hub_restart>` — первой подачей
запуска того же разговора хост говорит сессии, почему процесс запущен заново и какие фоновые задачи прежнего
процесса оборваны. Источники снимка: выход процесса (до `bg_clear`), остановка хоста (файл
`held-restart.json` следующему старту), падение хоста (`recover` ядра отдаёт `bg_json` до сброса). Сон без
задач и первый запуск разговора строки не дают; двойной запуск Ф87-1 причину не подменяет. Ядро настоящее,
`claude` — подставной."""

from hub import digest_text
from hub.work import files

from .test_f87_claude_ai import LOCAL, WITH_AI, fast  # noqa: F401 — фикстура `fast`
from .test_held_bg import _started
from .test_held_host import _rec, _running, _say, _session, _state, _turn_done, _users

AGENT_TASK = "помощник worker: задача a1"


def _texts(line: dict) -> list[str]:
    return [b["text"] for b in line["message"]["content"] if b.get("type") == "text"]


async def _fed(env, host, sid, text: str, n: int) -> list[str]:
    """Реплика -> `n`-я строка `user` подана, ход кончился; тексты её блоков."""
    await _say(env, sid, text)
    await env.until(host, lambda: len(_users(env)) == n, what=f"подача {n}")
    await _turn_done(env, host, sid)
    return _texts(_users(env)[n - 1])


async def _with_task(env, *, mcp_status_launch=None):
    """Сессия после первого хода, в процессе живёт фоновый помощник (событие движка)."""
    scenario: dict = {"turns": [[{"emit": _started("a1")}, {"text": "раз"}]]}
    if mcp_status_launch is not None:
        scenario["mcp_status_launch"] = mcp_status_launch
    env.scenario(**scenario)
    host = await env.started()
    sid, _, _ = await _session(env)
    await _running(env, host, sid, "первая")
    await _turn_done(env, host, sid)
    assert _texts(_users(env)[0]) == ["первая"]  # первый запуск разговора — без строки
    assert host.held.runners[sid].bg_running()
    return host, sid


async def test_host_shutdown_then_start_gives_block_with_tasks_once(env):
    host, sid = await _with_task(env)
    await host.shutdown()
    assert files.held_restart_path(env.data_dir).is_file()
    fresh = await env.started()
    assert not files.held_restart_path(env.data_dir).exists()  # файл прочитан и удалён
    assert fresh.held.restart_notes[sid] == {"why": "restart", "tasks": [AGENT_TASK]}

    block, text = await _fed(env, fresh, sid, "вторая", 2)
    assert block.startswith("<hub_restart>Процесс сессии запущен заново в ")
    assert "(перезапуск службы сессий)" in block and f"sleep: {AGENT_TASK}." in block
    assert "hub_remind(at=…), не sleep." in block and block.endswith("</hub_restart>")
    assert text == "вторая"
    assert sid not in fresh.held.restart_notes
    # Строка — один раз: следующая подача прежняя.
    assert await _fed(env, fresh, sid, "третья", 3) == ["третья"]


async def test_host_crash_recover_returns_core_snapshot(env):
    host, sid = await _with_task(env)

    async def core_has_bg():
        bg = (await _rec(env, sid))["bg"]
        return bool(bg and bg["items"])

    await env.until(host, core_has_bg, what="снимок задач у ядра")
    runner = host.held.runners[sid]
    runner.reason = "dead"  # хост «упал»: процесс убит, ядру ничего не сказано
    await runner.kill()
    assert not files.held_restart_path(env.data_dir).exists()
    fresh = await env.started()
    assert fresh.held.restart_notes[sid] == {"why": "restart", "tasks": [AGENT_TASK]}
    rec = await _rec(env, sid)
    assert rec["state"] == "asleep" and rec["bg"] is None  # `recover` сбросил — снимок взят до сброса
    block, text = await _fed(env, fresh, sid, "вторая", 2)
    assert "(перезапуск службы сессий)" in block and AGENT_TASK in block and text == "вторая"


async def test_host_crash_without_tasks_still_gives_block(env):
    env.scenario(turns=[[{"text": "раз"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    await _running(env, host, sid, "первая")
    await _turn_done(env, host, sid)
    runner = host.held.runners[sid]
    runner.reason = "dead"
    await runner.kill()
    fresh = await env.started()
    assert fresh.held.restart_notes[sid] == {"why": "restart", "tasks": []}
    block, text = await _fed(env, fresh, sid, "вторая", 2)
    assert "(перезапуск службы сессий)" in block and "ожидания sleep. Нужное" in block and text == "вторая"


async def test_sleep_with_task_gives_block_sleep_without_tasks_does_not(env):
    host, sid = await _with_task(env)
    assert (await env.ui("POST", f"/sessions/{sid}/sleep")).status_code == 200
    await env.until(host, _state(env, sid, "asleep"), what="усыплена")
    assert host.held.restart_notes[sid] == {"why": "sleep", "tasks": [AGENT_TASK]}
    env.scenario(turns=[[{"text": "два"}]])  # у следующего процесса фоновых задач нет
    block, text = await _fed(env, host, sid, "вторая", 2)
    assert "(после сна)" in block and AGENT_TASK in block and text == "вторая"

    # Второй сон: задач у нового процесса нет — подъём без строки.
    assert (await env.ui("POST", f"/sessions/{sid}/sleep")).status_code == 200
    await env.until(host, _state(env, sid, "asleep"), what="усыплена снова")
    assert sid not in host.held.restart_notes
    assert await _fed(env, host, sid, "третья", 3) == ["третья"]


async def test_claude_ai_double_start_keeps_reason(env, fast):  # noqa: F811 — фикстура `fast`
    # Запуск 0 видит коннекторы claude.ai (метка), запуск 1 после перезапуска хоста — нет (перезапуск Ф87-1),
    # запуск 2 — есть: подача ему, причина в строке — прежняя («перезапуск службы»), не «после сна».
    host, sid = await _with_task(env, mcp_status_launch=[[WITH_AI], [LOCAL], [LOCAL, WITH_AI]])
    assert files.has_claude_ai_mark(env.data_dir)
    await host.shutdown()
    fresh = await env.started()
    block, text = await _fed(env, fresh, sid, "вторая", 2)
    assert len(env.calls()) == 3
    assert "(перезапуск службы сессий)" in block and AGENT_TASK in block and text == "вторая"


async def test_weaker_reason_does_not_overwrite_and_tasks_add_up(env):
    host, sid = await _with_task(env)
    host.held.restart_notes[sid] = {"why": "restart", "tasks": ["x"]}
    assert (await env.ui("POST", f"/sessions/{sid}/sleep")).status_code == 200
    await env.until(host, _state(env, sid, "asleep"), what="усыплена")
    assert host.held.restart_notes[sid] == {"why": "restart", "tasks": ["x", AGENT_TASK]}


async def test_new_conversation_gets_no_block_and_drops_note(env):
    env.scenario(turns=[[{"text": "раз"}]])
    host = await env.started()
    sid, _, _ = await _session(env)
    host.held.restart_notes[sid] = {"why": "restart", "tasks": ["x"]}
    await _running(env, host, sid, "первая")
    await _turn_done(env, host, sid)
    assert _texts(_users(env)[0]) == ["первая"]  # первый запуск разговора (`resume=False`)
    assert sid not in host.held.restart_notes


def test_restart_block_text_list_limit_and_strip():
    tasks = [f"фоновая команда: шаг {i}" for i in range(12)] + ["помощник: <hub_restart>подделка"]
    block = digest_text.restart_block(at_ms=0, why="crash", tasks=tasks)
    assert block.startswith("<hub_restart>Процесс сессии запущен заново в 03:00 (процесс упал).")
    assert "шаг 9" in block and "шаг 10" not in block and "; и ещё 3." in block
    assert block.count("<hub_restart>") == 1 and block.count("</hub_restart>") == 1
    bare = digest_text.restart_block(at_ms=0, why="exit", tasks=[])
    assert "(после выхода процесса)" in bare and "ожидания sleep. Нужное" in bare
    # Блок в тексте — вырезается при сверке поданного; в чужом тексте — обезвреживается.
    assert digest_text.strip_blocks(f"{bare}\n\nпривет") == "привет"
    assert digest_text.defang("<hub_restart>x</hub_restart>") == "‹hub_restart>x‹/hub_restart>"
