"""Ф50-7, хост (docs/architecture.md, Р5–Р7 и поправки П4, П6, П8, П9): процесс сессии
хаба стартует в своей подпапке проекта; гарду — папка проекта и настоящая папка процесса; папка проверяется
перед каждым запуском; системный текст называет обе папки, без подпапки — прежний. Движки поддельные."""

import json
import shlex

from hub import roles, workdir_files
from hub.work import files, guard, say_files
from hub.work import held as held_mod

from .conftest import flag_value
from .oc_helpers import oc  # noqa: F401 — фикстура `oc`
from .oc_helpers import started as oc_started
from .test_held_host import _events, _rec, _running, _say, _session, _state, _turn_done
from .test_held_opencode import _oc_session
from .test_held_opencode import _running as _oc_running

SYSTEM = "--append-system-prompt"
SUB = "proekty/z 2 заказ"


def _project(env, *subs):
    pid, workdir = env.project("Initech")
    for sub in subs or (SUB,):
        (workdir / sub).mkdir(parents=True)
    return pid, workdir


def _guard_args(env, sid) -> list[str]:
    settings = json.loads(files.held_settings_path(env.data_dir, sid).read_text(encoding="utf-8"))
    [hook] = settings["hooks"]["PreToolUse"]
    return shlex.split(hook["hooks"][0]["command"])


async def _sleep(env, host, sid):
    r = await env.ui("POST", f"/sessions/{sid}/sleep", {})
    assert r.status_code == 200, r.text
    await env.until(host, _state(env, sid, "asleep"), what="сессия уснула")


async def _errors(env, sid) -> list[str]:
    return [e["summary"] for e in await _events(env, sid) if e["kind"] == "error"]


# --- запуск -------------------------------------------------------------------------------------------------


async def test_process_starts_in_the_subdir_and_guard_gets_both_folders(env):
    env.scenario(turns=[[{"text": "ок"}]])
    host = await env.started()
    pid, workdir = _project(env)
    sid, _, _ = await _session(env, project=(pid, workdir), role="программист", subdir=SUB + "/")
    assert (await _rec(env, sid))["subdir"] == SUB
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    [call] = env.calls()
    real = str((workdir / SUB).resolve())
    assert call["cwd"] == real  # процесс — в подпапке (с пробелом и кириллицей)
    args = _guard_args(env, sid)
    assert flag_value(args, "--workdir") == str(workdir)  # граница — папка ПРОЕКТА
    assert flag_value(args, "--home-dir") == real and "--profile" not in args  # Ф78-1: профиль один
    assert guard._parse_args(args[1:])[3] == real  # гард свой же аргумент разбирает
    system = flag_value(call["argv"], SYSTEM)
    assert f" Папка проекта: {workdir}. Твоя рабочая папка: {real} — подпапка проекта;" in system
    runner = host.held.runners[sid]
    assert runner.home_dir == real and runner.cwd == real and runner.workdir == str(workdir)
    assert (await _rec(env, sid))["failure"] is None and await _errors(env, sid) == []


async def test_session_without_subdir_launches_as_before(env):
    env.scenario(turns=[[{"text": "ок"}]])
    host = await env.started()
    sid, _pid, workdir = await _session(env, role="программист")
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    [call] = env.calls()
    assert call["cwd"] == str(workdir)
    assert "--home-dir" not in _guard_args(env, sid)  # П9: файл настроек прежний
    assert "Папка проекта:" not in flag_value(call["argv"], SYSTEM)
    runner = host.held.runners[sid]
    assert runner.home_dir is None and runner.cwd == str(workdir)


async def test_subdir_link_inside_the_project_goes_to_the_process_as_the_real_path(env):
    """П8: в записи — путь, как его написал владелец; процессу и гарду — настоящий."""
    env.scenario(turns=[[{"text": "ок"}]])
    host = await env.started()
    pid, workdir = _project(env)
    (workdir / "tekuschij").symlink_to(workdir / SUB)
    sid, _, _ = await _session(env, project=(pid, workdir), subdir="tekuschij")
    assert (await _rec(env, sid))["subdir"] == "tekuschij"
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    real = str((workdir / SUB).resolve())
    assert env.calls()[0]["cwd"] == real and flag_value(_guard_args(env, sid), "--home-dir") == real


async def test_folder_is_checked_before_every_launch(env):
    """Р3, П4: папка пропала или в ней появились настройки Claude Code — запуска нет, причина в сессии."""
    env.scenario(turns=[[{"text": "ок"}], [{"text": "ок"}]])
    host = await env.started(held_launch_retry_s=0)
    pid, workdir = _project(env, "proekty/z1")
    sid, _, _ = await _session(env, project=(pid, workdir), subdir="proekty/z1")
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    await _sleep(env, host, sid)
    assert len(env.calls()) == 1

    (workdir / "proekty" / ".mcp.json").write_text("{}")  # настройки выше папки, ниже корня проекта
    await _say(env, sid, "ещё")

    async def failed():
        return (await _rec(env, sid))["failure"] is not None

    await env.until(host, failed, what="отказ запуска")
    why = "в папке свои настройки движка: proekty/.mcp.json"
    rec = await _rec(env, sid)
    assert rec["failure"] == why and rec["state"] == "asleep" and rec["queued_says"] == 1
    assert f"запуск не удался: {why}" in await _errors(env, sid)
    assert len(env.calls()) == 1  # процесс не поднимался

    (workdir / "proekty" / ".mcp.json").unlink()
    (workdir / "proekty" / "z1").rmdir()
    await _say(env, sid, "и ещё")

    async def gone():
        return (await _rec(env, sid))["failure"] == "папки proekty/z1 нет в проекте"

    await env.until(host, gone, what="отказ: папки нет")
    assert len(env.calls()) == 1


async def test_self_check_counts_memory_of_the_process_folder_as_own(env):
    """П9: хост сверяет вызов гардом с той же папкой процесса, что и хук."""
    env.scenario(turns=[[{"text": "ок"}]])
    host = await env.started()
    pid, workdir = _project(env, "proekty/z1")
    sid, _, _ = await _session(env, project=(pid, workdir), subdir="proekty/z1")
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    runner = host.held.runners[sid]
    memory = env.home / ".claude" / "projects"
    own = memory / guard.memory_slug(str((workdir / "proekty/z1").resolve())) / "memory" / "a.md"
    foreign = memory / guard.memory_slug(str(workdir.parent / "drugoj")) / "memory" / "a.md"
    assert runner._self_check("Write", {"file_path": str(own), "content": "x"}) is None
    assert runner._self_check("Write", {"file_path": str(foreign), "content": "x"})[0] == "foreign_memory"
    runner.home_dir = None  # без папки процесса та же память — чужая
    assert runner._self_check("Write", {"file_path": str(own), "content": "x"})[0] == "foreign_memory"


async def test_oc_session_gets_the_subdir_as_directory(env, oc):  # noqa: F811
    oc.scenario([{"text": "Готово"}])
    host = await oc_started(env)
    pid, workdir = _project(env)
    sid, _, _ = await _oc_session(env, project=(pid, workdir), subdir=SUB)
    await _oc_running(env, host, oc, sid, "посмотри")
    await _turn_done(env, host, sid)
    real = str((workdir / SUB).resolve())
    assert oc.jsonl("calls.jsonl")[0]["query"]["directory"] == real
    [prompt] = oc.prompts()
    assert f"Твоя рабочая папка: {real}" in prompt["body"]["system"]


# --- тексты -------------------------------------------------------------------------------------------------


def test_system_text_without_subdir_is_byte_for_byte_the_old_one():
    """П6: вставка про две папки — единственное отличие текста сессии с подпапкой."""
    base = {"id": 5, "role": "программист", "role_no": 2, "engine": "claude", "role_file": "roli/a.md"}
    plain = held_mod.held_rules(
        {**base, "workdir": "/work/p", "cwd": "/work/p", "subdir": None}, "П", "", "Т"
    )
    assert plain == held_mod.held_rules(base, "П", "", "Т")
    assert "Папка проекта:" not in plain and held_mod.held_place(base) == ""
    assert plain.endswith(f"\n\n{roles.FILE_HEADER.format(path='roli/a.md')}\nТ")

    rec = {**base, "workdir": "/work/p", "cwd": "/work/p/proekty/z1", "subdir": "proekty/z1"}
    place = held_mod.held_place(rec)
    assert place.startswith(" Папка проекта: /work/p. Твоя рабочая папка: /work/p/proekty/z1 — подпапка")
    assert "Файл роли, hub-files и правила проекта (.claude/hub.md) — от папки проекта" in place
    header = roles.FILE_HEADER_SUBDIR.format(path="roli/a.md", project_dir="/work/p")
    with_sub = held_mod.held_rules(rec, "П", "", "Т")
    assert with_sub.endswith(f"\n\n{header}\nТ")
    old_header = roles.FILE_HEADER.format(path="roli/a.md")
    assert with_sub.replace(place, "", 1).replace(header, old_header) == plain
    assert with_sub.index("правилам проекта." + place) > 0
    # без пути процесса в записи — он собирается из папки проекта и подпапки
    assert "Твоя рабочая папка: /work/p/proekty/z1 " in held_mod.held_place({**rec, "cwd": None})


def test_saved_file_note_is_absolute_only_for_a_subdir_session(tmp_path):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    item = {"id": "f" * 32, "name": "a.bin", "mime": "application/octet-stream", "kind": "file", "size": 4}
    kw = dict(engine="claude", text="вот", files=[item], data={item["id"]: b"data"}, oc_image=False)
    plain = say_files.prepare(workdir=str(workdir), **kw)
    assert plain.text == f"вот\n\n{say_files.SAVED_NOTE}{workdir_files.DIR_NAME}/a.bin"
    (workdir / workdir_files.DIR_NAME / "a.bin").unlink()
    full = say_files.prepare(workdir=str(workdir), absolute=True, **kw)
    assert full.text == f"вот\n\n{say_files.SAVED_NOTE}{workdir}/{workdir_files.DIR_NAME}/a.bin"
    assert (workdir / workdir_files.DIR_NAME / "a.bin").read_bytes() == b"data"  # файл — в папке ПРОЕКТА
