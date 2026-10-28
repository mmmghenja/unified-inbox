"""Ф76-1 (П3, П10, П12): отпечаток, класс и дискретное решение — кодом, закрытыми словарями
(`core/decision_print.py`); проект внешней строки по папке (`decisions.project_of_cwd`)."""

import time

import pytest

from hub.core import decision_print as dp
from hub.core import decisions


@pytest.mark.parametrize(
    ("command", "rule", "expected"),
    [
        ("git push", None, "git push"),
        ("cd /x && git push origin main --force", None, "git push --force"),
        ("cd /x; git -C /y push", "push", "git push"),
        ("GH=abc timeout 30 nice -n 5 git push origin HEAD", None, "git push"),
        ("ls -la | grep x && rm -rf build", None, "rm -rf"),
        ("echo hi\nssh user@10.0.0.1 'uptime'", "server", "ssh 10.0.0.1"),
        ("git status && git push", "push", "git push"),
        ("npm run deploy -- --prod", None, "npm run deploy"),
        ("hub-load run --what x --cores 4 -- uv run pytest -q", None, "uv run pytest"),
        ("cat a.txt", None, "cat"),
        ("", None, ""),
        ("echo 'не закрыта", None, "echo"),
    ],
)
def test_first_command_without_values(command, rule, expected):
    assert dp.first_command(command, rule) == expected


def test_permission_print_shape():
    fp, cmd = dp.permission_print("Bash", {"command": "cd x && git push origin f1"}, "push")
    assert (fp, cmd) == ("permission|Bash|push|git push", "git push")
    # Не Bash — только инструмент и правило: пути и значения в отпечаток не идут.
    fp, cmd = dp.permission_print("Write", {"file_path": "/home/u/секрет.txt"}, "outside_workdir")
    assert (fp, cmd) == ("permission|Write|outside_workdir|", "")
    assert dp.permission_print(None, None, None)[0] == "permission|?|-|"


@pytest.mark.parametrize(
    ("text", "cls"),
    [
        ("Выкатить на прод?", "deploy"),
        ("Закоммитить и запушить?", "push"),
        ("Накатить миграцию 0106?", "migration"),
        ("Смержить ветку в master?", "merge"),
        ("Поставить тег v1.2?", "release"),
        ("Удалить старые ветки?", "delete"),
        ("Перезапустить hub-core?", "restart"),
        ("Отправить письмо клиенту?", "send"),
        ("Что думаешь про цвет кнопки?", None),
        ("", None),
        (None, None),
    ],
)
def test_classify_closed_dictionary(text, cls):
    assert dp.classify(text) == cls


@pytest.mark.parametrize(
    ("text", "verdict"),
    [
        ("да", "yes"),
        ("Да, пушь сам", "yes"),
        ("ок, делай всё", "yes"),
        ("+", "yes"),
        ("👍", "yes"),
        ("нет", "no"),
        ("не надо", "no"),
        ("не пушь", "no"),
        ("да нет, подожди", "no"),
        ("не", "no"),
        ("да, не пушь", "no"),
        # Ревью Ф76: «не» внутри согласия, не прямо перед ним, — не «нет».
        ("да не вопрос", None),
        ("можешь не спрашивать, пушь", None),
        ("ок, пушь не дожидаясь", None),
        ("да, не забудь тесты", None),
        ("стоп", "no"),
        ("да, но сначала тесты", None),
        ("сделай после обеда", None),
        ("посмотри логи", None),
        ("да " * 30, None),  # длиннее SHORT_MAX
        ("", None),
        (None, None),
    ],
)
def test_short_answer_verdict(text, verdict):
    assert dp.verdict_of_text(text) == verdict


def test_question_and_turn_prints():
    assert dp.question_print("Какой цвет, синий?") == ("question|-|какой цвет синий", None)
    assert dp.question_print("Выкатить на прод?")[1] == "deploy"
    assert dp.turn_print("Нужно запушить ветку — пушить?") == ("turn|push", "push")
    assert dp.turn_print(None) == ("turn|-", None)
    assert dp.classless("turn|-") and not dp.classless("turn|push")
    assert not dp.classless("question|-|какой цвет") and not dp.classless("permission|Write|-|")


def test_rule_of_uses_hub_guard(tmp_path):
    home = str(tmp_path)
    assert (
        dp.rule_of("Bash", {"command": "git push origin main"}, cwd=home, workdir=home, home=home) == "push"
    )
    assert dp.rule_of("Bash", {"command": "ls"}, cwd=home, workdir=home, home=home) is None
    assert dp.rule_of("Bash", "кривой вход", cwd=None, workdir=None, home=home) is None


def test_line_template_and_plural():
    ts = 1_760_000_000_000
    day = time.strftime("%d.%m", time.localtime(ts / 1000))
    line = decisions.line("permission", "permission|Bash|push|git push", None, "push", "no", 3, ts)
    assert line == f"git push/push → нет, 3 раза, последний {day}"
    line = decisions.line("turn", "turn|deploy", "deploy", None, "yes", 5, ts)
    assert line == f"deploy/turn → да, 5 раз, последний {day}"
    line = decisions.line("question", "question|-|какой цвет", None, None, "синий", 1, ts)
    assert line == f"question/question → «синий», 1 раз, последний {day}"
    assert decisions.line("join", "join|@x", None, None, "yes", 22, ts).endswith(f"22 раза, последний {day}")


def test_project_of_cwd_longest_prefix_and_temp_dirs():
    projects = [(1, "/home/u/Globex"), (2, "/home/u/Globex/projects/initech"), (3, None)]
    assert decisions.project_of_cwd("/home/u/Globex/src", projects) == 1
    assert decisions.project_of_cwd("/home/u/Globex/projects/initech/x", projects) == 2
    assert decisions.project_of_cwd("/home/u/Globex", projects) == 1
    assert decisions.project_of_cwd("/home/u/GlobexX", projects) is None
    assert decisions.project_of_cwd("/home/u/Globex/.runs/stand", projects) is None
    assert decisions.project_of_cwd("/tmp/claude-1000/x", projects) is None  # noqa: S108
    assert decisions.project_of_cwd("/dev/shm/x", [(9, "/dev/shm")]) is None
    assert decisions.project_of_cwd("относительный", projects) is None
    assert decisions.project_of_cwd(None, projects) is None
