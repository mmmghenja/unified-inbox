"""Ф48-3 (docs/architecture.md): файл роли из проекта в системном тексте постоянной сессии.
Файл читается при КАЖДОМ запуске процесса и кладётся дословно; не прочитался (нет файла, вышел за рабочую
папку, велик) — запуск без него и одно событие `warn`. Встроенных текстов ролей нет. Движок поддельный."""

import pytest

from hub import roles
from hub.work import held as held_mod

from .conftest import flag_value
from .test_held_host import _events, _rec, _running, _say, _session, _state, _turn_done

SYSTEM = "--append-system-prompt"
PATH = "docs/roles/ROLE.md"
HEADER = f"Инструкция роли (файл проекта {PATH}):"
ROLES_HEADER = "Роль и взаимодействие (правила хаба):"
OLD_BUILTIN = ("Ты — программист проекта.", "Ты — тестер проекта", "Ты — оркестратор проекта")


def _write(workdir, text, path=PATH):
    target = workdir / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _warns(events) -> list[str]:
    return [e["summary"] for e in events if e["kind"] == "warn" and "файл роли" in e["summary"]]


# --- сборка текста ------------------------------------------------------------------------------------------


def test_held_rules_puts_file_after_common_text_verbatim():
    body = "# Исследователь\n\n  - пункт с отступом  \n\nхвост\n"
    rec = {"id": 5, "role": "программист", "role_no": 2, "engine": "claude", "role_file": PATH}
    text = held_mod.held_rules(rec, "Проект", "ДОП", body)
    assert text.endswith(f"\n\n{ROLES_HEADER}\n{roles.common_text()}\n\n{HEADER}\n{body}")
    assert text.index("ДОП") < text.index(ROLES_HEADER) < text.index(HEADER)
    for old in OLD_BUILTIN:
        assert old not in text


def test_held_rules_without_file_text_has_no_block():
    rec = {"id": 5, "role": "тестер", "role_no": 1, "engine": "claude", "role_file": PATH}
    assert "Инструкция роли" not in held_mod.held_rules(rec, "Проект")
    assert "Инструкция роли" not in held_mod.held_rules({**rec, "role_file": None}, "Проект", "", "текст")


def test_held_rules_file_without_role():
    rec = {"id": 5, "role": None, "role_no": None, "engine": "claude", "role_file": PATH}
    text = held_mod.held_rules(rec, "Проект", "", "ТЕЛО")
    assert ROLES_HEADER not in text and text.endswith(f"\n\n{HEADER}\nТЕЛО")


# --- запуск процесса ----------------------------------------------------------------------------------------


async def _sleep(env, host, sid, what):
    r = await env.ui("POST", f"/sessions/{sid}/sleep", {})
    assert r.status_code == 200, r.text
    await env.until(host, _state(env, sid, "asleep"), what=what)


async def test_file_goes_into_system_text_and_is_reread_on_every_launch(env):
    env.scenario(turns=[[{"text": "первый"}]])
    host = await env.started()
    pid, workdir = env.project("Initech")
    first_body = "# Роль исследователя\n\nИщи, за что платят.\n  строка с отступом  \n"
    _write(workdir, first_body)
    sid, _, _ = await _session(env, project=(pid, workdir), role="программист", role_file=PATH)
    assert (await _rec(env, sid))["role_file"] == PATH
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    first = flag_value(env.calls()[0]["argv"], SYSTEM)
    assert f"{roles.common_text()}\n\n{HEADER}\n{first_body}" in first  # дословно, после общего текста
    for old in OLD_BUILTIN:
        assert old not in first
    assert _warns(await _events(env, sid)) == []

    # Правка файла у живого процесса ничего не перезапускает; следующий запуск читает файл заново.
    second_body = "# Роль исследователя, вторая редакция\n"
    _write(workdir, second_body)
    for _ in range(3):
        await env.cycle(host)
    assert len(env.calls()) == 1 and sid in host.held.runners
    await _sleep(env, host, sid, "спит")
    env.scenario(turns=[[{"text": "второй"}]])
    await _say(env, sid, "ещё")
    await env.until(host, lambda: len(env.calls()) == 2, what="второй запуск")
    second = flag_value(env.calls()[1]["argv"], SYSTEM)
    assert f"{HEADER}\n{second_body}" in second and "Ищи, за что платят." not in second
    await _turn_done(env, host, sid)

    # Файл снят у сессии — процесс сам не перезапускается; со следующего запуска блока нет.
    r = await env.ui("PATCH", f"/sessions/{sid}", {"role_file": ""})
    assert r.status_code == 200 and r.json()["session"]["role_file"] is None
    for _ in range(3):
        await env.cycle(host)
    assert sid in host.held.runners and host.held.runners[sid].reason is None
    await _sleep(env, host, sid, "спит снова")
    env.scenario(turns=[[{"text": "третий"}]])
    await _say(env, sid, "и ещё")
    await env.until(host, lambda: len(env.calls()) == 3, what="третий запуск")
    assert "Инструкция роли" not in flag_value(env.calls()[2]["argv"], SYSTEM)
    await _turn_done(env, host, sid)
    assert _warns(await _events(env, sid)) == []


def _missing(env, workdir):
    return PATH


def _symlink_out(env, workdir):
    outside = env.tmp / "outside-secret.md"
    outside.write_text("СЕКРЕТ-СНАРУЖИ", encoding="utf-8")
    (workdir / "docs").mkdir()
    (workdir / "docs" / "out.md").symlink_to(outside)
    return "docs/out.md"


def _dir_symlink_out(env, workdir):
    (env.tmp / "elsewhere").mkdir()
    (env.tmp / "elsewhere" / "R.md").write_text("СЕКРЕТ-СНАРУЖИ", encoding="utf-8")
    (workdir / "ext").symlink_to(env.tmp / "elsewhere")
    return "ext/R.md"


def _big(env, workdir):
    (workdir / "big.md").write_bytes(b"a" * (roles.FILE_MAX_BYTES + 1))
    return "big.md"


@pytest.mark.parametrize(
    ("prepare", "reason"),
    [
        (_missing, "нет файла"),
        (_symlink_out, "путь выходит за рабочую папку проекта"),
        (_dir_symlink_out, "путь выходит за рабочую папку проекта"),
        (_big, "файл больше 64 КиБ"),
    ],
)
async def test_unreadable_file_launches_without_it_and_warns_once(env, prepare, reason):
    env.scenario(turns=[[{"text": "работаю"}]])
    host = await env.started()
    pid, workdir = env.project("Initech")
    path = prepare(env, workdir)
    sid, _, _ = await _session(env, project=(pid, workdir), role="тестер", role_file=path)
    await _running(env, host, sid)
    await _turn_done(env, host, sid)
    system = flag_value(env.calls()[0]["argv"], SYSTEM)
    assert "Инструкция роли" not in system and "СЕКРЕТ-СНАРУЖИ" not in system
    assert roles.common_text() in system  # сессия запущена, общий текст на месте
    assert _warns(await _events(env, sid)) == [f"файл роли не прочитан: {reason}"]


async def test_path_from_record_is_checked_by_host_too(env):
    """Запись с путём наружу (мимо проверки ядра) хост не читает: граница — в общей функции чтения; и
    читает он файл один раз на запуск (повторная сборка системного текста — без второго `warn`)."""
    host = await env.started()
    pid, workdir = env.project("Initech")
    (env.tmp / "up.md").write_text("СЕКРЕТ-СНАРУЖИ", encoding="utf-8")
    sid, _, _ = await _session(env, project=(pid, workdir), role="тестер")
    base = {**(await _rec(env, sid)), "workdir": str(workdir)}
    for bad in ("../up.md", str(env.tmp / "up.md")):
        runner = held_mod.HeldRunner(host.held, {**base, "role_file": bad}, "Initech")
        seen = []
        runner.event = lambda kind, text, seen=seen, **kw: seen.append((kind, text))
        assert runner._role_file_text() == (bad, None)
        assert runner._role_file_text() == (bad, None)
        assert "СЕКРЕТ-СНАРУЖИ" not in runner._system_prompt()
        assert len(seen) >= 1 and seen[0][0] == "warn"
        assert [t for _k, t in seen if "файл роли" in t] == [seen[0][1]]
        assert seen[0][1].startswith("файл роли не прочитан: role_file:")
