"""Ф48-5 (docs/architecture.md): Claude Code закрепляет системный текст при создании
разговора. Хост пишет ядру снимок составляющих (режим, роль, файл роли) при запуске с `--session-id`, а при
запуске с `--resume` и после законченного сжатия подаёт отличия первым блоком первой подачи на вход. Нет
отличий или снимка — вход прежний байт в байт. Движок поддельный."""

import hashlib
import json

import pytest

from hub import roles
from hub.work import held as held_mod
from hub.work import session

from .conftest import flag_value
from .test_held_compact import _compacted, _compacts, _use
from .test_held_host import _events, _rec, _running, _say, _session, _state, _turn_done, _users
from .test_held_incoming import _core_rec, _no_debounce, _peer, _subscribe

PATH = "docs/roles/ROLE.md"
BODY = "# Роль\n\n  дословно, с отступом  \n"
HEAD = held_mod.PIN_FIX_HEAD


def _write(workdir, text, path=PATH):
    target = workdir / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _sha(text) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pin(env, sid) -> dict | None:
    raw = _core_rec(env, sid).sys_pin
    return None if raw is None else json.loads(raw)


def _content(line: dict) -> list[dict]:
    return line["message"]["content"]


def _fixes(env) -> list[str]:
    """Тексты поправок во всех подачах на вход (каждая — первым блоком своего сообщения)."""
    found = []
    for line in _users(env):
        blocks = _content(line)
        assert all(HEAD not in b.get("text", "") for b in blocks[1:])  # поправка — только первым блоком
        if blocks[0].get("text", "").startswith(HEAD):
            found.append(blocks[0]["text"])
    return found


async def _sleep(env, host, sid):
    r = await env.ui("POST", f"/sessions/{sid}/sleep", {})
    assert r.status_code == 200, r.text
    await env.until(host, _state(env, sid, "asleep"), what="спит")
    await env.until(host, lambda: sid not in host.held.runners, what="раннер снят")


async def _resumed_turn(env, host, sid, text):
    """Реплика спящей сессии: новый запуск процесса и законченный ход."""
    launches, users = len(env.calls()), len(_users(env))
    await _say(env, sid, text)
    await env.until(host, lambda: len(env.calls()) == launches + 1, what="запуск")
    await env.until(host, lambda: len(_users(env)) == users + 1, what="реплика подана")
    await _turn_done(env, host, sid)
    return _users(env)[-1]


# --- сборка поправки ----------------------------------------------------------------------------------------

# Ф56-1б (3.7): у сессии с ролью снимок несёт и sha256 общего текста ролей.
BASE = {
    "mode": "manual",
    "role": "тестер",
    "role_no": 1,
    "role_file": PATH,
    "file_sha": _sha(BODY),
    "common_sha": _sha(roles.common_text()),
}


def test_mode_paragraph_has_one_source():
    for mode in ("manual", "auto"):
        rec = {"id": 5, "role": None, "role_no": None, "engine": "claude", "mode": mode}
        assert f" {held_mod.mode_actions(mode)} " in held_mod.held_rules(rec, "Проект")
    assert held_mod.mode_actions("auto") != held_mod.mode_actions("manual")
    assert held_mod.mode_actions(None) == held_mod.mode_actions("manual")


def test_sys_pin_components():
    rec = {"mode": "auto", "role": "тестер", "role_no": 2}
    assert held_mod.sys_pin(rec, PATH, BODY) == {**BASE, "mode": "auto", "role_no": 2}
    # Файл не прочитан или пуст — блока в системном тексте нет: отпечатка нет, путь остаётся.
    assert held_mod.sys_pin(rec, PATH, None)["file_sha"] is None
    assert held_mod.sys_pin(rec, PATH, None)["role_file"] == PATH
    # Без роли и без файла.
    assert held_mod.sys_pin({"mode": "manual", "role": None, "role_no": 3}, None, "текст") == {
        "mode": "manual",
        "role": None,
        "role_no": None,
        "role_file": None,
        "file_sha": None,
    }


def test_pin_fix_no_snapshot_or_no_difference_is_none():
    for empty in (None, {}, "", []):
        assert held_mod.pin_fix(empty, {**BASE, "mode": "auto"}, BODY) is None  # закреплённое неизвестно
    assert held_mod.pin_fix(dict(BASE), dict(BASE), BODY) is None


def test_pin_fix_carries_only_changed_parts():
    def fix(text=BODY, **now):
        return held_mod.pin_fix(dict(BASE), {**BASE, **now}, text)

    auto = held_mod.mode_actions("auto")
    assert fix(mode="auto") == f"{HEAD}\n\n{auto}"
    assert held_mod.pin_fix({**BASE, "mode": "auto"}, dict(BASE), BODY) == (
        f"{HEAD}\n\n{held_mod.mode_actions('manual')}"
    )
    assert fix(role="программист", role_no=2) == f"{HEAD}\n\nТвоя роль теперь: «программист» 2"
    assert fix(role_no=3) == f"{HEAD}\n\nТвоя роль теперь: «тестер» 3"
    assert fix(role=None, role_no=None) == f"{HEAD}\n\nТвоя роль теперь: без роли"
    new_body = "новая редакция\n"
    assert fix(new_body, file_sha=_sha(new_body)) == f"{HEAD}\n\n{roles.file_block(PATH, new_body)}"
    assert fix(role_file="other.md") == f"{HEAD}\n\n{roles.file_block('other.md', BODY)}"  # сменился путь
    assert fix(None, role_file=None, file_sha=None) == f"{HEAD}\n\n{held_mod.PIN_FILE_GONE}"
    # Файл задан, но не читается — ничего (в ленте уже есть `warn`).
    assert fix(None, file_sha=None) is None
    assert fix(None, role_file="other.md", file_sha=None) is None
    # Несколько частей — в порядке: режим, роль, файл.
    assert fix(new_body, mode="auto", role=None, role_no=None, file_sha=_sha(new_body)) == (
        f"{HEAD}\n\n{auto}\n\nТвоя роль теперь: без роли\n\n{roles.file_block(PATH, new_body)}"
    )


# --- снимок при создании разговора --------------------------------------------------------------------------


async def test_first_launch_writes_snapshot_and_feeds_no_fix(env):
    env.scenario(turns=[[{"text": "первый"}]])
    host = await env.started()
    pid, workdir = env.project("Initech")
    _write(workdir, BODY)
    sid, _, _ = await _session(env, project=(pid, workdir), role="тестер", role_file=PATH)
    assert _pin(env, sid) is None
    say_id = await _running(env, host, sid, "посмотри")
    await _turn_done(env, host, sid)
    assert "--session-id" in env.calls()[0]["argv"]
    assert _pin(env, sid) == BASE
    # Вход — ровно та строка, что была до Ф48-5.
    assert _users(env) == [session.user_line("посмотри", message_uuid=session.held_say_uuid(say_id))]
    assert "sys_pin" not in await _rec(env, sid)  # интерфейсу снимок не отдаётся


async def test_resume_without_changes_keeps_input_byte_for_byte(env):
    env.scenario(turns=[[{"text": "первый"}]])
    host = await env.started()
    pid, workdir = env.project("Initech")
    _write(workdir, BODY)
    sid, _, _ = await _session(env, project=(pid, workdir), role="тестер", role_file=PATH)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    await _sleep(env, host, sid)
    env.scenario(turns=[[{"text": "второй"}]])
    launches = len(env.calls())
    say_id = await _say(env, sid, "ещё")
    await env.until(host, lambda: len(env.calls()) == launches + 1, what="запуск")
    await env.until(host, lambda: len(_users(env)) == 2, what="реплика подана")
    await _turn_done(env, host, sid)
    assert "--resume" in env.calls()[-1]["argv"]
    assert _users(env)[-1] == session.user_line("ещё", message_uuid=session.held_say_uuid(say_id))
    assert _fixes(env) == [] and host.held.runners[sid].pin_fix is None
    assert _pin(env, sid) == BASE  # `--resume` снимок не переписывает


async def test_no_snapshot_means_no_fix(env):
    """Сессия создана до Ф48-5: закреплённое неизвестно — поправок нет, снимок при `--resume` не пишется."""
    env.scenario(turns=[[{"text": "первый"}]])
    host = await env.started()
    pid, workdir = env.project("Initech")
    _write(workdir, BODY)
    sid, _, _ = await _session(env, project=(pid, workdir), role="тестер", role_file=PATH)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    await _sleep(env, host, sid)
    env.db.write_sync(lambda conn: conn.execute("UPDATE main.hub_session SET sys_pin = NULL"))
    _write(workdir, "совсем другой текст\n")
    env.scenario(turns=[[{"text": "второй"}]])
    line = await _resumed_turn(env, host, sid, "ещё")
    assert [b["text"] for b in _content(line)] == ["ещё"]
    assert _fixes(env) == [] and _pin(env, sid) is None


# --- поправка на вход ---------------------------------------------------------------------------------------


async def test_changed_file_goes_first_block_once_per_launch_and_again_after_compaction(env):
    env.scenario(
        turns=[[{"text": "раз"}, _use(20_000)]],
        init_model="claude-test",
    )
    host = await env.started(held_compact_min_growth=0, held_compact_tokens=150_000)
    pid, workdir = env.project("Initech")
    _write(workdir, BODY)
    sid, _, _ = await _session(env, project=(pid, workdir), role="тестер", role_file=PATH)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    await _sleep(env, host, sid)

    # Правка файла сама сессию не будит и хода не создаёт.
    new_body = "# Роль, вторая редакция\n\nСекретное число роли: 7731\n"
    _write(workdir, new_body)
    launches, users = len(env.calls()), len(_users(env))
    for _ in range(4):
        await env.cycle(host)
    assert len(env.calls()) == launches and len(_users(env)) == users
    assert (await _rec(env, sid))["state"] == "asleep"

    # Запуск с `--resume`: поправка — первым блоком первой подачи, реплика — вторым, тем же сообщением.
    env.scenario(
        turns=[[{"text": "два"}, _use(30_000)], [{"text": "три"}, _use(160_000)], [{"text": "четыре"}]],
        compact={"pre_tokens": 161_000},
        init_model="claude-test",
    )
    expected = f"{HEAD}\n\n{roles.file_block(PATH, new_body)}"
    say_id = await _say(env, sid, "ещё")
    await env.until(host, lambda: len(env.calls()) == launches + 1, what="запуск")
    await env.until(host, lambda: len(_users(env)) == users + 1, what="реплика подана")
    await _turn_done(env, host, sid)
    assert "--resume" in env.calls()[-1]["argv"]
    line = _users(env)[-1]
    assert line["uuid"] == session.held_say_uuid(say_id)  # то же сообщение, uuid реплики
    assert _content(line) == [{"type": "text", "text": expected}, {"type": "text", "text": "ещё"}]
    assert "7731" in expected and held_mod.mode_actions("manual") not in expected  # только изменившееся
    assert _pin(env, sid)["file_sha"] == _sha(BODY)  # снимок — про создание разговора, не про поправку
    turn_ends = [e["kind"] for e in await _events(env, sid)].count("turn_end")
    assert turn_ends == 2  # отдельного хода поправка не создала

    # Вторая подача того же процесса — без поправки; этот ход доводит контекст до порога сжатия.
    await _say(env, sid, "дальше")
    await env.until(host, lambda: len(_users(env)) >= users + 2, what="вторая реплика")
    assert [b["text"] for b in _content(_users(env)[users + 1])] == ["дальше"]
    await _compacted(env, host, sid)
    assert _compacts(env) == 1
    compact_line = _users(env)[-1]
    assert [b["text"] for b in _content(compact_line)] == [
        "/compact"
    ]  # команде сжатия поправка не предшествует
    assert len(_fixes(env)) == 1

    # Первая подача ПОСЛЕ законченного сжатия — снова с поправкой, и снова один раз.
    await _say(env, sid, "после сжатия")
    await env.until(host, lambda: len(_users(env)) == users + 4, what="реплика после сжатия")
    await _turn_done(env, host, sid)
    assert [b["text"] for b in _content(_users(env)[-1])] == [expected, "после сжатия"]
    assert _fixes(env) == [expected, expected]


async def _to_auto(env, sid, workdir):
    r = await env.ui("PATCH", f"/sessions/{sid}", {"mode": "auto"})
    assert r.status_code == 200, r.text
    return held_mod.mode_actions("auto")


async def _to_other_role(env, sid, workdir):
    r = await env.ui("PATCH", f"/sessions/{sid}", {"role": "программист"})
    assert r.status_code == 200, r.text
    return "Твоя роль теперь: «программист» 1"


async def _to_no_role(env, sid, workdir):
    r = await env.ui("PATCH", f"/sessions/{sid}", {"role": None})
    assert r.status_code == 200, r.text
    return "Твоя роль теперь: без роли"


async def _to_other_path(env, sid, workdir):
    _write(workdir, BODY, "other/ROLE.md")  # текст тот же, путь другой
    r = await env.ui("PATCH", f"/sessions/{sid}", {"role_file": "other/ROLE.md"})
    assert r.status_code == 200, r.text
    return roles.file_block("other/ROLE.md", BODY)


async def _to_no_file(env, sid, workdir):
    r = await env.ui("PATCH", f"/sessions/{sid}", {"role_file": None})
    assert r.status_code == 200, r.text
    return held_mod.PIN_FILE_GONE


@pytest.mark.parametrize("change", [_to_auto, _to_other_role, _to_no_role, _to_other_path, _to_no_file])
async def test_each_changed_part_and_only_it(env, change):
    env.scenario(turns=[[{"text": "первый"}]])
    host = await env.started()
    pid, workdir = env.project("Initech")
    _write(workdir, BODY)
    sid, _, _ = await _session(env, project=(pid, workdir), role="тестер", role_file=PATH)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    await _sleep(env, host, sid)
    part = await change(env, sid, workdir)
    env.scenario(turns=[[{"text": "второй"}]])
    line = await _resumed_turn(env, host, sid, "ещё")
    assert "--resume" in env.calls()[-1]["argv"]
    assert [b["text"] for b in _content(line)] == [f"{HEAD}\n\n{part}", "ещё"]
    assert _pin(env, sid) == BASE


async def test_unreadable_file_gives_no_fix_only_warn(env):
    env.scenario(turns=[[{"text": "первый"}]])
    host = await env.started()
    pid, workdir = env.project("Initech")
    _write(workdir, BODY)
    sid, _, _ = await _session(env, project=(pid, workdir), role="тестер", role_file=PATH)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    await _sleep(env, host, sid)
    (workdir / PATH).unlink()
    env.scenario(turns=[[{"text": "второй"}]])
    line = await _resumed_turn(env, host, sid, "ещё")
    assert [b["text"] for b in _content(line)] == ["ещё"] and _fixes(env) == []
    warns = [
        e["summary"] for e in await _events(env, sid) if e["kind"] == "warn" and "файл роли" in e["summary"]
    ]
    assert warns == ["файл роли не прочитан: нет файла"]


async def test_fix_rides_on_a_signal_and_does_not_wake_by_itself(env):
    """Первая подача после `--resume` — сигнал хаба: поправка идёт тем же вводом первым блоком."""
    env.scenario(turns=[[{"text": "первый"}]])
    host = await env.started()
    pid, workdir = env.project("Initech")
    _write(workdir, BODY)
    sid, _, _ = await _session(env, project=(pid, workdir), role="маркетолог", role_file=PATH)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    await _subscribe(env, sid, workdir)
    await _sleep(env, host, sid)
    new_body = "вторая редакция\n"
    _write(workdir, new_body)
    launches = len(env.calls())
    for _ in range(4):
        await env.cycle(host)
    assert len(env.calls()) == launches  # сама поправка сессию не подняла
    env.scenario(turns=[[{"text": "прочитала"}]])
    _no_debounce(env)
    await _peer(env, pid)
    await env.until(host, lambda: len(env.calls()) == launches + 1, what="подъём по сигналу")
    await env.until(host, lambda: len(_users(env)) == 2, what="сигнал подан")
    await _turn_done(env, host, sid)
    blocks = _content(_users(env)[-1])
    assert blocks[0]["text"] == f"{HEAD}\n\n{roles.file_block(PATH, new_body)}"
    assert len(blocks) == 2 and blocks[1]["text"].startswith("Сигнал хаба")
    assert len(_users(env)) == 2  # один ввод, отдельного хода нет


# --- передача дел: новый разговор, новый снимок -------------------------------------------------------------


async def test_handoff_writes_snapshot_anew_and_has_no_fix(env):
    env.scenario(turns=[[{"text": "первый"}]])
    host = await env.started()
    pid, workdir = env.project("Initech")
    _write(workdir, BODY)
    old, _, _ = await _session(env, project=(pid, workdir), role="тестер", role_file=PATH)
    await _running(env, host, old)
    await _turn_done(env, host, old)
    await _sleep(env, host, old)
    # Правила изменились после создания старого разговора.
    new_body = "редакция для новой сессии\n"
    _write(workdir, new_body)
    r = await env.ui("POST", f"/sessions/{old}/handoff", {"mode": "auto"})
    assert r.status_code == 200, r.text
    new = r.json()["session"]["id"]
    assert new != old and _pin(env, new) is None
    env.scenario(turns=[[{"text": "принял дела"}]])
    launches = len(env.calls())
    await env.until(host, lambda: len(env.calls()) > launches, what="запуск новой сессии")
    runner = host.held.runners[new]
    await env.until(host, lambda: runner.turn_seen and not runner.busy, what="первый ход новой")
    argv = env.calls()[-1]["argv"]
    assert "--session-id" in argv and "--resume" not in argv
    assert new_body in flag_value(argv, "--append-system-prompt")
    assert _pin(env, new) == {**BASE, "mode": "auto", "file_sha": _sha(new_body)}
    assert _fixes(env) == [] and runner.pin_fix is None
