"""Ф41-3 (docs/architecture.md): подробности событий постоянной сессии
на уровне функций `hub.work.session` — без хоста и процессов."""

import json

import pytest

from hub.work import session

WD = "/home/u/proj"


def test_rel_path_inside_outside_and_odd():
    assert session.rel_path(f"{WD}/src/a.py", WD) == "src/a.py"
    assert session.rel_path(f"{WD}/./src/../a.py", WD) == "a.py"
    assert session.rel_path(WD, WD) == "."
    assert session.rel_path("/home/u/proj-2/a.py", WD) == "/home/u/proj-2/a.py"  # сосед, а не вложенный
    assert session.rel_path("/etc/hosts", WD) == "/etc/hosts"
    assert session.rel_path("src/a.py", WD) == "src/a.py"  # уже относительный — как есть
    assert session.rel_path(f"{WD}/a.py", None) == f"{WD}/a.py"
    assert session.rel_path(None, WD) is None


def test_summarize_tool_use_is_relative_only_with_workdir():
    edit = {"file_path": f"{WD}/src/a.py"}
    assert session.summarize_tool_use("Edit", edit, WD) == "src/a.py"
    assert session.summarize_tool_use("Grep", {"path": f"{WD}/src", "pattern": "x"}, WD) == "src · x"
    assert session.summarize_tool_use("Read", {"file_path": "/etc/hosts"}, WD) == "/etc/hosts"
    # Задача по «Го» рабочую папку не передаёт — строка прежняя.
    assert session.summarize_tool_use("Edit", edit) == f"{WD}/src/a.py"


def test_tool_input_detail_table():
    detail = session.tool_input_detail
    assert detail("Bash", {"command": "ls -la"}) is None  # команда целиком в строке события
    assert detail("Bash", {"command": "ls", "description": "Список"}) == {
        "type": "input",
        "command": "ls",
        "description": "Список",
    }
    long = "echo начало\n" + "x=1; " * 100
    got = detail("Bash", {"command": long})
    assert got == {"type": "input", "command": long}  # длиннее строки события (и многострочная)
    assert detail("Bash", {"command": "TOKEN=abc123 curl x", "description": "d"})["command"] == (
        "TOKEN=*** curl x"
    )
    assert len(detail("Bash", {"command": "я" * 30_000, "description": "d"})["command"]) == 20_000
    for name in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Read"):
        assert detail(name, {"file_path": "/a", "content": "тайна"}) is None
    assert detail("Grep", {"pattern": "x", "path": "/a"}) == {
        "type": "input",
        "input": {"pattern": "x", "path": "/a"},
    }
    masked = detail("mcp__hub__hub_draft", {"text": "password hunter2", "n": [1, {"k": "api_key=zzz"}]})
    assert masked["input"] == {"text": "password=***", "n": [1, {"k": "api_key=***"}]}
    assert detail("Task", {"prompt": "я" * 20_000}) == {"type": "input", "input": {"_cut": True}}
    assert detail("Glob", {}) is None and detail("Glob", None) is None


HUNKS = [
    {"oldStart": 1, "oldLines": 2, "newStart": 1, "newLines": 3, "lines": [" a", "-b", "+B", "+c"]},
    {"oldStart": 10, "oldLines": 1, "newStart": 11, "newLines": 0, "lines": ["-z"]},
]


def _done(name, tool_input, content="ok", use_result=None, is_error=False):
    block = {"type": "tool_result", "content": content, "is_error": is_error}
    return session.tool_result_details(name, tool_input, block, use_result, WD)


def test_diff_from_structured_patch():
    brief, detail = _done(
        "Edit",
        {"file_path": f"{WD}/src/a.py"},
        use_result={"filePath": f"{WD}/src/a.py", "structuredPatch": HUNKS},
    )
    assert brief == {"type": "diff", "file": "src/a.py", "added": 2, "removed": 2}
    assert detail == {
        "type": "diff",
        "file": "src/a.py",
        "patch": "@@ -1,2 +1,3 @@\n a\n-b\n+B\n+c\n@@ -10,1 +11,0 @@\n-z",
        "cut": False,
    }
    # Путь — из входа, если в результате его нет. Приёмка 41-3: тело diff не маскируется — маска портила
    # честный код (`token = load()`).
    hunks = [
        {"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1, "lines": ["-token = load()", "+x = 1"]}
    ]
    brief, detail = _done("MultiEdit", {"file_path": "/etc/app.conf"}, use_result={"structuredPatch": hunks})
    assert brief["file"] == "/etc/app.conf" and detail["patch"].splitlines()[1] == "-token = load()"


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".env.local",
        "prod.env",
        "id_ed25519",
        "server.pem",
        "tg.session",
        "credentials.json",
        "secrets.yaml",
        ".netrc",
    ],
)
def test_diff_of_secret_file_keeps_counts_only(name):
    """Приёмка 41-3: diff файла-хранилища секретов в события не пишется — остаётся строка «+N −M»."""
    lines = ["-TOKEN=aaa111", "+TOKEN=bbb222"]
    hunks = [{"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1, "lines": lines}]
    brief, detail = _done("Edit", {"file_path": f"{WD}/{name}"}, use_result={"structuredPatch": hunks})
    assert brief == {"type": "diff", "file": name, "added": 1, "removed": 1} and detail is None
    new = {"type": "create", "filePath": f"{WD}/{name}", "content": "TOKEN=aaa111\n", "structuredPatch": []}
    assert _done("Write", {"file_path": f"{WD}/{name}"}, use_result=new)[1] is None


CODE_NAMES = ["tokens.py", "test_secrets.py", "password_reset.py", "environment.ts", "keys.md"]


@pytest.mark.parametrize("name", CODE_NAMES)
def test_code_files_with_secret_like_names_keep_diff(name):
    hunks = [{"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1, "lines": ["-a", "+b"]}]
    _brief, detail = _done("Edit", {"file_path": f"{WD}/{name}"}, use_result={"structuredPatch": hunks})
    assert detail is not None and detail["patch"].endswith("+b")


def test_diff_of_new_file_and_its_line_limit():
    use_result = {"type": "create", "filePath": f"{WD}/n.txt", "content": "раз\nдва\n", "structuredPatch": []}
    brief, detail = _done("Write", {"file_path": f"{WD}/n.txt"}, use_result=use_result)
    assert brief == {"type": "diff", "file": "n.txt", "added": 2, "removed": 0}
    assert detail == {"type": "diff", "file": "n.txt", "patch": "@@ -0,0 +1,2 @@\n+раз\n+два", "cut": False}
    big = {"type": "create", "content": "\n".join(f"s{i}" for i in range(1000))}
    brief, detail = _done("Write", {"file_path": f"{WD}/big.txt"}, use_result=big)
    assert brief["added"] == 1000 and detail["cut"] is True  # в строке — сколько всего, в diff — 400
    assert len(detail["patch"].splitlines()) == 1 + session.NEW_FILE_LINES_MAX
    # Перезапись тем же содержимым: ни патча, ни «нового файла» — подробностей нет.
    assert _done("Write", {}, use_result={"type": "update", "content": "x", "structuredPatch": []}) == (
        None,
        None,
    )
    # Содержимое без патча у Edit новым файлом не считается.
    assert _done("Edit", {}, use_result={"content": "x"}) == (None, None)


def test_diff_from_opencode_patch_text_and_cut():
    patch = (
        "Index: /home/u/proj/app.py\n==========\n--- /home/u/proj/app.py\n+++ /home/u/proj/app.py\n"
        "@@ -1,2 +1,2 @@\n def f():\n-    return nam\n+    return name\n"
    )
    brief, detail = _done("Edit", {}, use_result={"patch": patch, "filePath": f"{WD}/app.py"})
    assert brief == {"type": "diff", "file": "app.py", "added": 1, "removed": 1}
    assert detail["patch"] == "@@ -1,2 +1,2 @@\n def f():\n-    return nam\n+    return name"
    # Патч на два файла: шапка второго файла тоже уходит; строка «--- » внутри тела остаётся строкой diff.
    two = patch + "Index: b.py\n=====\n--- b.py\n+++ b.py\n@@ -1 +1 @@\n--- старый заголовок\n+новый\n"
    _brief, detail = _done("Edit", {}, use_result={"patch": two})
    assert detail["patch"].splitlines()[-3:] == ["@@ -1 +1 @@", "--- старый заголовок", "+новый"]
    assert "Index" not in detail["patch"]
    # Длинный патч режется по строкам; счёт в строке ленты — по всему патчу.
    lines = [f"+{'я' * 99}" for _ in range(1000)]
    hunks = [{"oldStart": 1, "oldLines": 0, "newStart": 1, "newLines": 1000, "lines": lines}]
    brief, detail = _done("Edit", {"file_path": f"{WD}/a"}, use_result={"structuredPatch": hunks})
    assert brief["added"] == 1000 and detail["cut"] is True
    assert len(detail["patch"]) <= session.PATCH_MAX
    assert len(json.dumps(detail, ensure_ascii=False).encode()) <= 60_000  # предел ядра — в байтах
    assert _done("Edit", {}, use_result={"patch": "без заголовков"}) == (None, None)


def test_no_fields_no_details():
    # П11: нет `tool_use_result` (другая версия движка), битая форма — событие без подробностей.
    for use_result in (None, "строка", {}, {"structuredPatch": "x"}, {"structuredPatch": [{"lines": 1}]}):
        assert _done("Edit", {"file_path": "/a"}, use_result=use_result) == (None, None)
    assert _done("Edit", {}, use_result={"structuredPatch": [{"oldStart": "x", "lines": []}]}) == (None, None)
    # Вызов не найден (мог быть Read) и сам Read — содержимое файла в события не пишется.
    assert _done(None, None, content="содержимое файла") == (None, None)
    assert _done("Read", {"file_path": "/a"}, content="содержимое файла") == (None, None)


def test_output_of_bash_other_tools_and_errors():
    out = "\n".join(f"строка {i}" for i in range(2000))
    brief, detail = _done("Bash", {"command": "x"}, content=out)
    assert brief == {"type": "output", "lines": 2000}
    assert detail["cut"] is True and len(detail["text"]) == 8000 and detail["text"].endswith("строка 1999")
    brief, detail = _done("Grep", {"pattern": "x"}, content=out)
    assert detail["cut"] is True and len(detail["text"]) == 4000 and detail["text"].startswith("строка 0")
    assert _done("Bash", {}, content="a\nb\n") == (
        {"type": "output", "lines": 2},
        {"type": "output", "text": "a\nb\n", "cut": False},
    )
    assert _done("Bash", {}, content="") == ({"type": "output", "lines": 0}, None)
    assert _done("Bash", {}, content="export API_KEY=abc\nok")[1]["text"] == "export API_KEY=***\nok"
    # Ошибка — у любого инструмента (и у Read, и у правки): текст ошибки, начало до 4000.
    for name in ("Read", "Edit", "Bash", None):
        brief, detail = _done(name, {}, content="нет файла\n" + "е" * 5000, is_error=True)
        assert brief == {"type": "output", "lines": 2}
        assert (
            detail["text"].startswith("нет файла") and len(detail["text"]) == 4000 and detail["cut"] is True
        )
    # Список блоков вместо строки (как отдаёт Claude Code у MCP).
    listed = [{"type": "text", "text": "раз"}, {"type": "image"}, {"type": "text", "text": "два"}]
    assert _done("mcp__x__y", {}, content=listed)[1]["text"] == "раз два"


def test_fit_detail_cuts_by_bytes():
    detail = session.fit_detail({"type": "output", "text": "я" * 40_000, "cut": False})
    assert detail["cut"] is True and len(json.dumps(detail, ensure_ascii=False).encode()) <= 60_000
    assert detail["text"] == "я" * len(detail["text"]) and len(detail["text"]) > 20_000
    same = {"type": "output", "text": "x" * 8000, "cut": False}
    assert session.fit_detail(same) is same
    assert session.fit_detail({"type": "input", "input": {"a": "я" * 40_000}}) is None  # резать нечего
    assert session.fit_detail(None) is None


RESULT = {
    "type": "result",
    "duration_ms": 12_345,
    "num_turns": 5,
    "total_cost_usd": 0.5,
    "usage": {
        "input_tokens": 26,
        "output_tokens": 454,
        "cache_read_input_tokens": 184_883,
        "cache_creation_input_tokens": 68_251,
        "iterations": [
            {"input_tokens": 10, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 5},
            {"input_tokens": 8, "cache_read_input_tokens": 86_907, "cache_creation_input_tokens": 266},
        ],
    },
    "modelUsage": {
        "claude-haiku": {"contextWindow": 200_000},
        "claude-sonnet": {"contextWindow": 1_000_000},
    },
}


def test_turn_usage_claude_cost_is_a_difference():
    brief, usage, total = session.turn_usage(RESULT, prev_total=None, model="claude-haiku")
    assert brief == {
        "type": "usage",
        "duration_ms": 12_345,
        "steps": 5,
        "cost_usd": 0.5,  # первый ход процесса — само значение
        "tokens": {"input": 26, "output": 454, "cache_read": 184_883, "cache_write": 68_251},
    }
    assert usage == {"context_tokens": 87_181, "context_limit": 200_000, "turn_cost_usd": 0.5}
    assert total == 0.5
    brief, usage, total = session.turn_usage({**RESULT, "total_cost_usd": 0.8}, prev_total=total, model="x")
    assert brief["cost_usd"] == 0.3 and usage["turn_cost_usd"] == 0.3 and total == 0.8
    assert usage["context_limit"] is None  # модель хода неизвестна, а в списке их две
    # Счётчик процесса пошёл заново (меньше прежнего) — цена хода равна значению.
    brief, _usage, total = session.turn_usage({**RESULT, "total_cost_usd": 0.1}, prev_total=0.8)
    assert brief["cost_usd"] == 0.1 and total == 0.1


def test_turn_usage_fallbacks_and_absent_fields():
    no_iterations = {**RESULT, "usage": {"input_tokens": 1, "output_tokens": 2}, "modelUsage": {"m": {}}}
    delta = {"input_tokens": 10, "cache_read_input_tokens": 14_823, "cache_creation_input_tokens": 64_182}
    brief, usage, _ = session.turn_usage(no_iterations, prev_total=None, delta_usage=delta)
    assert usage == {"context_tokens": 79_015, "context_limit": None, "turn_cost_usd": 0.5}
    assert brief["tokens"] == {"input": 1, "output": 2, "cache_read": 0, "cache_write": 0}
    # Ни `iterations`, ни `message_delta` — расход сессии не шлётся; строка хода остаётся.
    brief, usage, _ = session.turn_usage(no_iterations, prev_total=None)
    assert usage is None and brief["steps"] == 5
    # Единственная модель в списке — она и есть модель хода.
    one = {**RESULT, "modelUsage": {"m": {"contextWindow": 200_000}}}
    assert session.turn_usage(one, prev_total=None)[1]["context_limit"] == 200_000
    # П11: в `result` нет ни расхода, ни цены — подробностей нет, ничего не падает.
    bare = {"type": "result", "duration_ms": 10, "num_turns": 1}
    brief, usage, total = session.turn_usage(bare, prev_total=0.4)
    assert brief == {"type": "usage", "duration_ms": 10, "steps": 1, "cost_usd": None, "tokens": None}
    assert usage is None and total == 0.4
    assert session.turn_usage({"type": "result"}, prev_total=None) == (None, None, None)
    odd = {
        "duration_ms": "долго",
        "num_turns": True,
        "total_cost_usd": "дорого",
        "usage": [1],
        "modelUsage": 5,
    }
    assert session.turn_usage(odd, prev_total=None) == (None, None, None)


def test_turn_usage_opencode_cost_is_already_per_turn():
    msg = {
        "engine": "opencode",
        "duration_ms": 900,
        "num_turns": 2,
        "total_cost_usd": 0.01,
        "usage": {
            "input": 100,
            "output": 20,
            "reasoning": 3,
            "total": 500,
            "cache_read": 7,
            "cache_write": 1,
        },
        "context_tokens": 66_204,
        "context_limit": 1_000_000,
    }
    brief, usage, total = session.turn_usage(msg, prev_total=5.0)
    assert brief["cost_usd"] == 0.01 and total == 5.0  # разность с прошлым ходом не берётся
    assert brief["tokens"] == {"input": 100, "output": 20, "cache_read": 7, "cache_write": 1}
    assert usage == {"context_tokens": 66_204, "context_limit": 1_000_000, "turn_cost_usd": 0.01}
    brief, usage, _ = session.turn_usage(
        {**msg, "context_tokens": None, "context_limit": None}, prev_total=None
    )
    assert usage is None and brief["cost_usd"] == 0.01


def test_build_argv_partial_flag_is_opt_in():
    base = {
        "claude": "claude",
        "settings": "/s.json",
        "model": "m",
        "system_prompt": "p",
        "session_flag": "--session-id",
        "session_id": "x",
    }
    plain = session.build_argv(**base)
    assert "--include-partial-messages" not in plain  # задачи по «Го» и оркестратор — как раньше
    partial = session.build_argv(**base, partial=True)
    assert [a for a in partial if a != "--include-partial-messages"] == plain
    assert partial[-2:] == ["--session-id", "x"]


def test_build_argv_effort_only_known_levels():
    base = {
        "claude": "claude",
        "settings": "/s.json",
        "model": "m",
        "system_prompt": "p",
        "session_flag": "--session-id",
        "session_id": "x",
    }
    plain = session.build_argv(**base)
    argv = session.build_argv(**base, effort="high")
    at = argv.index("--effort")
    assert argv[at + 1] == "high" and argv[:at] + argv[at + 2 :] == plain
    for bad in (None, "", "ultra", "--resume"):
        assert session.build_argv(**base, effort=bad) == plain
