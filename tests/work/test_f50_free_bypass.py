"""Ревью Ф50-3 (находка «сессия переписывает свой файл свобод одной командой»; исходный тест ревьюера —
`.runs/critic-f50/test_free_file_bypass.py`). Набор больше не лежит там, куда может писать процесс сессии:
он в памяти хоста, хук спрашивает его по сокету. Ни одна из команд ревьюера — пропустил её гард или нет —
не меняет набор, который видит гард; пути к набору у гарда нет."""

import base64
import io
import json
import os
import subprocess

import pytest

from hub.work import guard

from .conftest import make_repo

EVERY = sorted(set(guard.RULES) - {guard.FAILURE})
PAYLOAD = json.dumps(EVERY)
# Слова ключей («push», «ssh») в тексте команды сами дают карточку — сессия передаст набор в base64.
B64 = base64.b64encode(PAYLOAD.encode()).decode()
SID = 7


@pytest.fixture(params=["under_home", "stand"])
def box(tmp_path, request, monkeypatch, free_sets):
    # Временный каталог pytest лежит под /tmp, а /tmp у гарда открыт без карточки — убираем эту поблажку,
    # чтобы стенд не был мягче боевой раскладки.
    monkeypatch.setattr(guard, "OPEN_WRITE_ROOTS", ("/nonexistent-open-root",))
    home = tmp_path / "home"
    workdir = home / "proj"
    make_repo(workdir)
    data = home / ".local" / "share" / "hub" if request.param == "under_home" else tmp_path / ".runs" / "data"
    held = data / "work" / f"held-{SID}"
    held.mkdir(parents=True)
    (held / "settings.json").write_text("{}")
    return {
        "home": str(home),
        "wd": str(workdir),
        "data": str(data),
        "work": str(data / "work"),
        "held": str(held),
        "free": free_sets,
    }


def _argv(box) -> list[str]:
    return ["--workdir", box["wd"], "--profile", "held", *box["free"].args(SID)]


def _guard(box, command):
    event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": box["wd"]}
    return guard.run(_argv(box), io.StringIO(json.dumps(event)), home=box["home"])


def _seen(box) -> frozenset:
    keys, why = guard.read_free_sock(*guard._parse_args(_argv(box))[2])
    assert why is None, why
    return keys


def _commands(box) -> dict[str, str]:
    work, wd = box["work"], box["wd"]
    half = len(work) // 2
    rel = os.path.relpath(box["held"], box["home"])  # путь каталога сессии от домашней папки
    cut = rel.rfind("/work/") - 1  # имя каталога данных пополам: текстовое правило его не узнаёт
    return {
        "home_var_split": f"A=$HOME/{rel[:cut]}; B={rel[cut:]}; echo {B64} | base64 -d > $A$B/free.json",
        "home_python": (
            "python3 -c \"import base64,os;open(os.path.expanduser('~/"
            + rel[:cut]
            + "'+'"
            + rel[cut:]
            + f"/free.json'),'w').write(base64.b64decode('{B64}').decode())\""
        ),
        "var_prefix": f'D={work}; echo {B64} | base64 -d > "$D/held-{SID}/free.json"',
        "var_split": f"A={work[:half]}; B={work[half:]}; echo {B64} | base64 -d > $A$B/held-{SID}/free.json",
        "mv_ancestor": (
            f"mv {work} {wd}/w && echo {B64} | base64 -d > {wd}/w/held-{SID}/free.json && mv {wd}/w {work}"
        ),
        "cd_var": f"W={work}; cd $W/held-{SID} && echo {B64} | base64 -d > free.json",
        "python_relative": (
            f"cd {box['data']} && python3 -c \"open('wo'+'rk/held-{SID}/free.json','w')"
            f".write(__import__('base64').b64decode('{B64}').decode())\""
        ),
        # То же самое по настройкам процесса сессии, где раньше лежал путь к набору.
        "settings_rewrite": f"W={work}; echo {B64} | base64 -d > $W/held-{SID}/settings.json",
    }


NAMES = [
    "home_var_split",
    "home_python",
    "var_prefix",
    "var_split",
    "mv_ancestor",
    "cd_var",
    "python_relative",
    "settings_rewrite",
]


# Наборы, которые владелец реально может оставить: ничего не снято; сняты только «каталоги хаба» и «запись вне
# проекта» (push, server и прочее остаются под карточкой — их сессия и добывала себе сама).
@pytest.mark.parametrize("free", [[], ["hub_dirs", "outside_workdir"]], ids=["none", "hub_dirs"])
@pytest.mark.parametrize("name", NAMES)
def test_no_command_changes_the_set_the_guard_sees(box, free, name):
    box["free"].sets[SID] = list(free)
    assert _seen(box) == frozenset(free)
    command = _commands(box)[name]
    # Пропустил гард команду или нет — выполняем её так, как выполнил бы движок: худший случай.
    subprocess.run(  # noqa: S603
        ["bash", "-c", command],  # noqa: S607
        cwd=box["wd"],
        env={**os.environ, "HOME": box["home"]},
        check=False,
        capture_output=True,
    )
    assert _seen(box) == frozenset(free), f"сессия расширила себе свободы ({name})"
    # Следующий вызов хука с той же командной строкой идёт по прежнему набору.
    code, out, _err = _guard(box, "git push")
    assert code == 0 and "[hub-rule:push]" in out
    assert _guard(box, "pkill -f hub-work")[0] == guard.EXIT_DENY


def test_guard_command_line_names_no_path_to_the_set(box):
    """Командная строка хука несёт имя сокета, pid и id — ни одного пути, кроме рабочей папки."""
    argv = _argv(box)
    paths = [word for word in argv if word.startswith("/")]
    assert paths == [box["wd"]]
    name = argv[argv.index("--free-sock") + 1]
    assert "/" not in name and not os.path.exists(name)
    assert not hasattr(guard, "read_free_file")
