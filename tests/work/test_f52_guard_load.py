"""Ф52-1 (docs/architecture.md): гард и обёртка `hub load run … -- <команда>`.

Граница безопасности: обёртка не должна стать способом обойти гард. Команда под обёрткой проверяется теми
же правилами, что без неё: вердикт и ключ правила совпадают — в обоих профилях, во всех формах записи
обёртки. Сам `hub load` (показать, `run`, `add`, `done`) гард пропускает.

Временное git-репо: основное рабочее дерево и связанное (`git worktree add`), как в `test_work_guard.py`.
"""

import contextlib
import io

import pytest

from hub.work import guard

from .conftest import git, make_repo

FLAGS = '--what "полный прогон" --cores 4 --mem-gb 6 --minutes 25'
# Формы обёртки: `{}` — место команды.
WRAPPERS = [
    f"hub load run {FLAGS} -- {{}}",
    f"hub load run {FLAGS} --who оркестратор -- {{}}",
    f"hub load run {FLAGS} {{}}",  # без `--`: команда — первый позиционный аргумент
    "hub load run --what=прогон --cores=4 --mem-gb=6 --minutes=25 -- {}",
    "hub load run --what=прогон --cores=4 --mem-gb=6 --minutes=25 {}",
    f"/home/owner/projects/hub-prod/.venv/bin/hub load run {FLAGS} -- {{}}",
    f"env HUB_X=1 timeout 600 hub load run {FLAGS} -- {{}}",
    f"cd . && hub load run {FLAGS} -- {{}}",
    f"hub load --json run {FLAGS} -- {{}}",
    f"uv run hub load run {FLAGS} -- {{}}",
    # второй вход того же CLI — `hub-load` (доступен сессии хаба при любом положении `hub_cli`)
    f"hub-load run {FLAGS} -- {{}}",
    f"hub-load run {FLAGS} --who оркестратор -- {{}}",
    f"hub-load run {FLAGS} {{}}",
    "hub-load run --what=прогон --cores=4 --mem-gb=6 --minutes=25 {}",
    f"/home/owner/projects/hub-prod/.venv/bin/hub-load run {FLAGS} -- {{}}",
    f"env HUB_X=1 timeout 600 /srv/x/.venv/bin/hub-load run {FLAGS} -- {{}}",
    f"hub-load --json run {FLAGS} -- {{}}",
]
# Команды, которые без обёртки дают карточку или отказ (хотя бы в одном профиле).
GUARDED = [
    "git push origin main",
    "git -C sub push",
    "docker push registry/app:1",
    "ssh host uptime",
    "scp a host:b",
    "rsync -a a host:b",
    "timeout 5 ssh host",
    'bash -lc "ssh host uptime"',
    "bash -lc 'git push origin main'",
    'bash -lc "echo x > /etc/f52-outside"',
    'sh -c "cd /srv && touch f52-outside"',
    "cp notes.txt /etc/f52-outside",
    "tee /etc/f52-outside",
    "sed -i s/a/b/ CLAUDE.md",
    "git checkout main",
    "kubectl apply -f k.yaml",
    "gh pr merge 5",
    "./deploy.sh prod",
    "make deploy-stage",
    "pkill -f claude",
    "systemctl --user restart hub-core.service",
    "bash scripts/hub-deploy.sh",
    "claude -p задача",
    "npx opencode run x",
]
HARMLESS = ["uv run pytest -q -n 4", "npm run build", "docker build -t app .", "git status", "ls -la"]


@pytest.fixture
def repos(tmp_path):
    home = tmp_path / "home"
    (home / ".claude" / "projects" / "p" / "memory").mkdir(parents=True)
    workdir = tmp_path / "project"
    main = make_repo(workdir)
    linked = main / ".claude" / "worktrees" / "w"
    git("worktree", "add", "-q", "-b", "fix", str(linked), cwd=main)
    return {"home": home, "workdir": workdir, "linked": linked}


def _verdict(repos, command, free=frozenset(), cwd=None):
    """(вид, ключ правила) или ("pass", None)."""
    event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd or repos["workdir"])}
    try:
        guard.decide(event, workdir=str(repos["workdir"]), home=str(repos["home"]), free=free)
    except guard.Verdict as verdict:
        return verdict.kind, verdict.key
    return "pass", None


# --- обёртка не обходит правила --------------------------------------------------------------------------


@pytest.mark.parametrize("command", GUARDED)
def test_wrapped_command_gets_same_verdict(repos, command):
    bare = _verdict(repos, command)
    assert bare[0] in ("ask", "deny"), command  # список проверяет сам себя
    for wrapper in WRAPPERS:
        wrapped = wrapper.format(command)
        assert _verdict(repos, wrapped) == bare, wrapped


def test_spec_example_push_stays_card(repos):
    """Пример спеки: `hub load run … -- git push origin main` — карточка, как без обёртки."""
    assert _verdict(repos, "git push origin main") == ("ask", "push")
    assert _verdict(repos, f"hub load run {FLAGS} -- git push origin main") == ("ask", "push")


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("ssh host uptime", ("ask", "server")),
        ('bash -lc "ssh host uptime"', ("ask", "server")),
        ("cp notes.txt /etc/f52-outside", ("ask", "outside_workdir")),
        ('bash -lc "echo x > /etc/f52-outside"', ("ask", "outside_workdir")),
        ("pkill -f claude", ("deny", "kill_agents")),
        ("bash scripts/hub-deploy.sh", ("deny", "hub_scripts")),
        ("systemctl --user restart hub-core.service", ("deny", "hub_units")),
        ("claude -p задача", ("ask", "nested_agent")),
    ],
)
def test_wrapped_named_rules_held(repos, command, expected):
    """Правила, которые ловятся только по позиции команды (без обёртки-исключения прошли бы мимо)."""
    assert _verdict(repos, f"hub load run {FLAGS} -- {command}") == expected


def test_wrapped_deny_beats_card(repos):
    """Отказ по команде под обёрткой сильнее карточки по соседней части команды."""
    command = f"git push; hub load run {FLAGS} -- pkill -f claude"
    assert _verdict(repos, command) == ("deny", "kill_agents")


def test_wrapped_main_tree_git(repos):
    """`git checkout` в основном дереве — карточка и под обёрткой; в связанном — свободно."""
    wrapped = f"hub load run {FLAGS} -- git checkout main"
    assert _verdict(repos, wrapped) == ("ask", "main_tree_git")
    assert _verdict(repos, wrapped, cwd=repos["linked"]) == ("pass", None)


def test_wrapped_follows_autopilot_switch(repos):
    """Снятый выключатель автопилота снимает правило и под обёрткой — ровно как без неё, не больше."""
    free = frozenset({"push"})
    assert _verdict(repos, "git push", free=free) == ("pass", None)
    assert _verdict(repos, f"hub load run {FLAGS} -- git push", free=free) == ("pass", None)
    assert _verdict(repos, f"hub load run {FLAGS} -- ssh host", free=free) == ("ask", "server")


# --- CLI хаба под обёрткой — отказ всегда ---------------------------------------------------------------

HUB_INSIDE = [
    "hub status",
    "hub engine set opencode",
    "hub load",
    "/home/owner/projects/hub-prod/.venv/bin/hub autopilot delegate 3 off",
    "./.venv/bin/hub map list",
    "hub-load",
    "/home/owner/projects/hub-prod/.venv/bin/hub-load done 3",
    "python -m hub status",
    "python3 -m hub.cli status",
    ".venv/bin/python3.14 -X dev -m hub status",
    "env HUB_X=1 timeout 60 hub status",
    "uv run hub status",
    "uv run --project /srv/x hub status",
    "uv run -- hub status",
    "uv run python -m hub status",
    'bash -lc "hub status"',
    "sh -c 'cd . && hub-load done 3'",
    f"hub load run {FLAGS} -- ls",  # обёртка в обёртке
    f"hub-load run {FLAGS} -- ls",
    # ревью Ф52-1: ключи `uv run` со значением и без, модуль питона слитно и в связке ключей
    "uv run --project /srv/x -q hub status",
    "uv run --project /srv/x --no-sync hub status",
    "uv run --no-sync --project /srv/x --directory /srv/y hub status",
    "uv run -q --project /srv/x hub status",
    "uv run --project=/srv/x --frozen --with rich hub status",
    "uv run -m hub status",
    "uv run --no-sync --module hub.cli status",
    "python -Bm hub.cli status",
    "python -mhub.cli status",
    "python3 -X dev -W ignore -Im hub status",
    "nice hub status",
    "xargs hub status",
    "command hub status",
    "sh -c 'exec hub status'",
    "bash -c 'bash -c \"bash -c hub\\ status\"'",
]
OUTER = [f"hub load run {FLAGS} --", f"hub-load run {FLAGS} --", f"/srv/x/.venv/bin/hub-load run {FLAGS}"]


@pytest.mark.parametrize("inner", HUB_INSIDE)
def test_hub_cli_under_wrapper_is_denied_always(repos, inner):
    """Обёртка не обходит выключатель `hub_cli`: CLI хаба под ней — отказ при любом наборе свобод (это не
    правило таблицы — ключа нет, снять нечем)."""
    for outer in OUTER:
        command = f"{outer} {inner}"
        for free in (frozenset(), frozenset(guard.RULES)):
            assert _verdict(repos, command, free=free) == ("deny", None), command


def test_hub_cli_under_wrapper_reason(repos):
    event = {"tool_name": "Bash", "tool_input": {"command": f"hub-load run {FLAGS} -- hub status"}}
    event["cwd"] = str(repos["workdir"])
    with pytest.raises(guard.Verdict) as caught:
        guard.decide(event, workdir=str(repos["workdir"]), home=str(repos["home"]))
    assert "запускай hub напрямую, не через обёртку" in caught.value.reason


def test_hub_word_that_is_not_command_stays_free(repos):
    """`hub` аргументом (папка, имя) — не CLI хаба: отказа нет."""
    for inner in ("uv run pytest -q tests/hub", "ls src/hub", "uv run python -m pytest hub", "echo hub"):
        assert _verdict(repos, f"hub-load run {FLAGS} -- {inner}") == ("pass", None), inner


@pytest.mark.parametrize(
    "inner",
    [
        # ревью Ф52-1: значение ключа `uv run` — не команда (проект лежит в папке `hub`)
        "uv run --project /home/owner/projects/hub pytest -q",
        "uv run --directory /home/owner/projects/hub pytest -q -n 4",
        "uv run --project hub pytest -q",
        "uv run --no-sync --with hub --python 3.14 pytest -q",
        "uv run --project=hub -q pytest tests/hub",
        "python -m pytest hub",
        "python -c 'print(1)' -m hub",
        "python script.py -m hub",
    ],
)
def test_project_named_hub_is_not_hub_cli(repos, inner):
    assert _verdict(repos, inner) == ("pass", None)
    for outer in OUTER:
        assert _verdict(repos, f"{outer} {inner}") == ("pass", None), (outer, inner)


# --- слово `run`, собранное оболочкой; команда, начинающаяся с ключа (ревью Ф52-1) --------------------------

SHELL_RUN = [
    ("R=run; hub-load $R {} -- ssh host uptime", ("ask", "server")),
    ("hub-load $'run' {} -- ssh host uptime", ("ask", "server")),
    ("R=run; hub-load $R {} -- cp notes.txt /etc/f52-outside", ("ask", "outside_workdir")),
    ("hub-load $'run' {} -- cp notes.txt /etc/f52-outside", ("ask", "outside_workdir")),
    ("hub-load $'run' {} -- kubectl apply -f k.yaml", None),
    ("hub-load $'run' {} -- pkill -f claude", ("deny", "kill_agents")),
    ("hub-load $'run' {} -- systemctl --user restart hub-core.service", ("deny", "hub_units")),
    ("R=run; hub load $R {} -- ssh host", ("ask", "server")),
    ("R=run; /srv/x/.venv/bin/hub-load $R {} -- hub status", ("deny", None)),
]


@pytest.mark.parametrize(("command", "expected"), SHELL_RUN)
def test_tail_after_dashes_checked_without_literal_run(repos, command, expected):
    """`run` подставит оболочка — гард его не видит; хвост после `--` у `hub-load` / `hub load` проверяется
    всё равно."""
    got = _verdict(repos, command.format(FLAGS))
    assert got[0] in ("ask", "deny"), (command, got)
    if expected is not None:
        assert got == expected, command


@pytest.mark.parametrize(
    "command",
    [
        f"hub-load run {FLAGS} -- -x ssh host",
        f"hub-load run {FLAGS} -- --what ssh host",
        f"hub load run {FLAGS} -- -",
        f"hub-load run {FLAGS} -1 ssh host",
        f"hub-load run {FLAGS} --unknown ssh host",
        f"hub-load run {FLAGS} - ssh host",
        f"R=run; hub-load $R {FLAGS} -- -x",
    ],
)
def test_command_starting_with_dash_is_failure_card(repos, command):
    """Команда под обёрткой начинается с дефиса (или до неё незнакомый ключ): что запустится, решает разбор
    CLI, а не гард — карточка под ключом сбоя, выключателями не снимается."""
    free = frozenset(guard.RULES) - {"hub_dirs"}
    assert _verdict(repos, command, free=free) == ("ask", guard.FAILURE), command
    assert _verdict(repos, command, "task")[0] in ("ask", "deny"), command


def test_dashes_inside_command_are_not_a_card(repos):
    """Второй `--` — часть самой команды (`pytest -- -k x`): карточки нет; сокращённые ключи обёртки —
    тоже."""
    assert _verdict(repos, f"hub-load run {FLAGS} -- uv run pytest -q -- -k x") == ("pass", None)
    short = "hub-load run --wh прогон --co 4 --me 6 --mi=25 --who я uv run pytest -q"
    assert _verdict(repos, short) == ("pass", None)
    assert _verdict(repos, short.replace("uv run pytest -q", "ssh host")) == ("ask", "server")


def test_guard_finds_what_cli_parser_runs():
    """Ревью Ф52-1, дифференциальная проверка: команда, которую запустил бы разбор `hub-load run …`, всегда
    среди кандидатов гарда (либо начинается с дефиса — тогда карточка сбоя)."""
    import random

    from hub import cli as hub_cli

    parser = hub_cli._build_parser()  # noqa: SLF001
    alphabet = [
        "--what", "--what=a", "--cores", "--cores=1", "--mem-gb", "--mem-gb=1", "--minutes", "--minutes=5",
        "--who", "--who=w", "--", "a", "1", "ssh", "host", "-1", "-", "--wh", "--co", "--json", "run", "x y",
        "-x y", "--mi=5", "--me", "=", "--what=", "-h=1",
    ]  # fmt: skip
    required = ["--what", "a", "--cores", "1", "--mem-gb", "1", "--minutes", "5"]
    rnd = random.Random(52)  # noqa: S311 — не криптография: воспроизводимый перебор
    ran = 0
    for _ in range(20000):
        extra = [rnd.choice(alphabet) for _ in range(rnd.randint(0, 5))]
        pos = rnd.randint(0, len(extra))
        head = required if rnd.random() < 0.8 else [rnd.choice(alphabet) for _ in range(rnd.randint(0, 8))]
        argv = [*(["--json"] if rnd.random() < 0.1 else []), "run", *extra[:pos], *head, *extra[pos:]]
        try:
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                args = parser.parse_args(["load", *argv])
        except SystemExit:
            continue
        real = list(args.command)
        if real[:1] == ["--"]:
            real = real[1:]
        if not real:
            continue
        ran += 1
        candidates, odd = guard._hub_load_scan(["hub-load", *argv])  # noqa: SLF001
        assert real in candidates or (real[0].startswith("-") and odd), (argv, real, candidates)
    assert ran > 500


def test_too_many_nested_wrappers_is_not_bypass(repos):
    """Обёртки цепочкой: вложенная обёртка — сама CLI хаба, отказ; свободы его не снимают."""
    command = " ".join([f"hub load run {FLAGS} --"] * 20) + " ssh host"
    assert _verdict(repos, command) == ("deny", None)
    free = frozenset(guard.RULES) - {"hub_dirs"}
    assert _verdict(repos, command, free=free) == ("deny", None)


def test_too_many_candidates_is_card(repos):
    """Кандидатов в команду больше предела — карточка под ключом сбоя; выключателями она не снимается."""
    command = f"hub-load run {FLAGS} " + "-- " * 20 + "ls"
    free = frozenset(guard.RULES) - {"hub_dirs"}
    assert _verdict(repos, command, free=free) == ("ask", guard.FAILURE)


# --- сам `hub load` и безобидные команды под обёрткой -----------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "hub load",
        "hub load --json",
        f"hub load add --pid 4242 {FLAGS}",
        "hub load done 3",
        "/home/owner/projects/hub-prod/.venv/bin/hub load",
        "hub-load",
        "hub-load --json",
        f"hub-load add --pid 4242 {FLAGS}",
        "hub-load done 3",
        "/home/owner/projects/hub-prod/.venv/bin/hub-load",
        *(f"/home/owner/projects/hub-prod/.venv/bin/hub-load run {FLAGS} -- {inner}" for inner in HARMLESS),
        *(f"hub load run {FLAGS} -- {inner}" for inner in HARMLESS),
        # значение ключа, похожее на команду, — не команда
        'hub load run --what "ssh" --cores 1 --mem-gb 0 --minutes 5 --who kubectl -- uv run pytest -q',
        'hub load run --what "scp -- ssh host" --cores 1 --mem-gb 0 --minutes 5 -- npm test',
    ],
)
def test_hub_load_itself_passes_guard(repos, command):
    assert _verdict(repos, command) == ("pass", None), command


def test_inner_commands_found():
    words = ["hub", "load", "run", "--what", "x", "--cores", "1", "--", "ssh", "host"]
    assert guard._hub_load_commands(words) == [["ssh", "host"]]  # noqa: SLF001
    words = ["hub", "load", "run", "--what", "x", "ssh", "host", "--", "a"]
    assert guard._hub_load_commands(words) == [["ssh", "host", "--", "a"], ["a"]]  # noqa: SLF001
    for words in (["hub", "load"], ["hub", "load", "add", "--pid", "1"], ["hub", "status"], ["load", "run"]):
        assert guard._hub_load_commands(words) == []  # noqa: SLF001
    words = ["/x/.venv/bin/hub-load", "run", "--what", "x", "--cores", "1", "--", "ssh", "host"]
    assert guard._hub_load_commands(words) == [["ssh", "host"]]  # noqa: SLF001
    assert guard._hub_load_commands(["hub-load", "done", "3"]) == []  # noqa: SLF001
