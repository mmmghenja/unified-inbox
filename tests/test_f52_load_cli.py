"""Ф52-1 (docs/architecture.md): CLI `hub load`. Главное требование — запись не
остаётся висеть: `hub load run … -- <команда>` снимает её при успехе, при ошибке команды и при SIGTERM,
выходит с кодом команды, а при недоступном ядре всё равно запускает команду.

Ядро — настоящий `hub core` в отдельном процессе на временных каталогах и свободном порту (как в
`tests/test_cli.py`); `hub load` запускается отдельным процессом — так же, как его зовут сессии.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

_STARTUP_TIMEOUT_S = 10.0

_HUB = [sys.executable, "-c", "from hub.cli import main; main()"]
_CLAIM = ["--what", "полный прогон", "--cores", "1", "--mem-gb", "1.5", "--minutes", "20"]


def _wait_for_file(path: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise TimeoutError(f"{path} не появился за {timeout} с")


def _wait_for_listening_socket(path: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(path))
        except OSError:
            time.sleep(0.02)
        else:
            return
        finally:
            probe.close()
    raise TimeoutError(f"по {path} никто не слушает спустя {timeout} с")


def _free_port() -> int:
    """Свободный порт вместо боевого 8787: тесты не мешают живому хабу и не зависят от него."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def env(tmp_path):
    """Окружение с временными каталогами хаба; ядро не запущено."""
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    data_dir.mkdir()
    config_dir.mkdir()
    return {**os.environ, "HUB_DATA_DIR": str(data_dir), "HUB_CONFIG_DIR": str(config_dir)}


@pytest.fixture
def core(env):
    proc = subprocess.Popen(  # noqa: S603 — фиксированный локальный интерпретатор
        [*_HUB, "core", "--port", str(_free_port())],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_file(Path(env["HUB_CONFIG_DIR"]) / "admin.key", _STARTUP_TIMEOUT_S)
        _wait_for_listening_socket(Path(env["HUB_DATA_DIR"]) / "internal.sock", _STARTUP_TIMEOUT_S)
        yield env
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _hub(env, *argv, cwd=None, timeout=30) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 — фиксированный локальный интерпретатор
        [*_HUB, *argv], env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout
    )


def _claims(env) -> list[dict]:
    result = _hub(env, "load", "--json")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["claims"]


def _wait_claims(env, count: int, timeout: float = 10.0) -> list[dict]:
    deadline = time.monotonic() + timeout
    while True:
        claims = _claims(env)
        if len(claims) == count or time.monotonic() > deadline:
            return claims
        time.sleep(0.1)


# --- run: запись на время команды ------------------------------------------------------------------------


def test_run_claims_while_running_and_releases_on_success(core, tmp_path):
    """Команда внутри обёртки видит запись (со своими полями), после выхода записи нет; вывод — насквозь."""
    workdir = tmp_path / "Umbrella"
    workdir.mkdir()
    inner = (
        "import subprocess, sys; "
        f"r = subprocess.run({_HUB!r} + ['load', '--json'], capture_output=True, text=True); "
        "sys.stdout.write(r.stdout); sys.exit(r.returncode)"
    )
    result = _hub(core, "load", "run", *_CLAIM, "--", sys.executable, "-c", inner, cwd=workdir)
    assert result.returncode == 0, result.stderr
    claims = json.loads(result.stdout)["claims"]
    assert len(claims) == 1
    claim = claims[0]
    # `--who` по умолчанию — имя текущей папки; память — в МБ
    assert (claim["who"], claim["what"]) == ("Umbrella", "полный прогон")
    assert (claim["cores"], claim["mem_mb"], claim["minutes"]) == (1, 1536, 20)
    assert _claims(core) == []


def test_run_releases_on_command_failure_and_keeps_exit_code(core):
    result = _hub(
        core, "load", "run", *_CLAIM, "--who", "hub", "--", sys.executable, "-c", "import sys; sys.exit(7)"
    )
    assert result.returncode == 7
    assert _claims(core) == []


def test_run_command_not_found_releases(core):
    result = _hub(core, "load", "run", *_CLAIM, "--", "hub-f52-no-such-command")
    assert result.returncode == 127
    assert "hub-f52-no-such-command" in result.stderr
    assert _claims(core) == []


def test_run_command_flags_pass_through(core):
    """Ключи команды после `--` не съедает разбор `hub load run` (в том числе одноимённые)."""
    code = "import sys; print(sys.argv[1:])"
    result = _hub(core, "load", "run", *_CLAIM, "--", sys.executable, "-c", code, "--minutes", "3", "-q")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "['--minutes', '3', '-q']"


def test_run_without_command_is_usage_error(core):
    result = _hub(core, "load", "run", *_CLAIM, "--")
    assert result.returncode != 0
    assert "нужна команда" in result.stderr
    assert _claims(core) == []


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_run_forwards_signal_and_releases(core, tmp_path, signum):
    """SIGTERM/SIGINT обёртке: сигнал пересылается ребёнку, запись снимается, код выхода — 128 + сигнал."""
    marker = tmp_path / "child.pid"
    child = f"import os, time; open({str(marker)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    proc = subprocess.Popen(  # noqa: S603 — фиксированный локальный интерпретатор
        [*_HUB, "load", "run", *_CLAIM, "--who", "hub", "--", sys.executable, "-c", child],
        env=core,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,  # сигнал получает только обёртка — ребёнку его должна передать она
    )
    try:
        _wait_for_file(marker, 15)
        claims = _wait_claims(core, 1)
        assert [c["pid"] for c in claims] == [proc.pid]
        deadline = time.monotonic() + 5
        while not marker.read_text() and time.monotonic() < deadline:
            time.sleep(0.02)
        child_pid = int(marker.read_text())
        proc.send_signal(signum)
        assert proc.wait(timeout=15) == 128 + signum
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    assert _claims(core) == []
    # ребёнок не осиротел: сигнал до него дошёл
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(child_pid, signal.SIGKILL)
        pytest.fail("ребёнок пережил сигнал обёртке")


def test_run_killed_hard_is_cleaned_by_pid(core, tmp_path):
    """`kill -9` обёртке: снять запись некому — её убирает чистка по pid при следующем чтении."""
    marker = tmp_path / "child.pid"
    child = f"import os, time; open({str(marker)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    proc = subprocess.Popen(  # noqa: S603 — фиксированный локальный интерпретатор
        [*_HUB, "load", "run", *_CLAIM, "--", sys.executable, "-c", child],
        env=core,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        _wait_for_file(marker, 15)
        assert len(_wait_claims(core, 1)) == 1
        proc.kill()
        proc.wait(timeout=5)
        assert _claims(core) == []
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # осиротевший ребёнок теста
        except ProcessLookupError:
            pass


def test_run_without_core_still_runs_command(env):
    """Ядро недоступно (деплой, сбой): команда запускается, код её, одна строка в stderr."""
    code = "print('сделано'); raise SystemExit(3)"
    result = _hub(env, "load", "run", *_CLAIM, "--", sys.executable, "-c", code)
    assert result.returncode == 3
    assert result.stdout.strip() == "сделано"
    # Без пользовательского systemd (CI) обёртка добавляет строку «systemd-run недоступен» — её не считаем.
    lines = [ln for ln in result.stderr.strip().splitlines() if not ln.startswith("hub-load: systemd-run")]
    assert lines == ["хаб недоступен — запуск без записи"]


def test_run_core_refusal_still_runs_command(core):
    """Ядро отказало в записи (минут больше предела): учёт не ломает работу — команда идёт без записи."""
    argv = ["--what", "x", "--cores", "1", "--mem-gb", "0", "--minutes", "500"]
    result = _hub(core, "load", "run", *argv, "--", sys.executable, "-c", "print('сделано')")
    assert result.returncode == 0
    assert result.stdout.strip() == "сделано"
    assert "запуск без записи" in result.stderr and "minutes" in result.stderr


# --- add / done / показать -------------------------------------------------------------------------------


def test_add_done_and_text(core):
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])  # noqa: S603
    try:
        result = _hub(
            core, "load", "add", "--pid", str(sleeper.pid), "--what", "стенд", "--cores", "2", "--mem-gb",
            "6", "--minutes", "30", "--who", "Umbrella / оркестратор",
        )  # fmt: skip
        assert result.returncode == 0, result.stderr
        claim_id = int(result.stdout.strip())
        assert [c["pid"] for c in _claims(core)] == [sleeper.pid]

        text = _hub(core, "load").stdout
        lines = text.strip().splitlines()
        assert lines[0].startswith("ядер ") and "загрузка" in lines[0] and "свободно" in lines[0]
        assert lines[1].strip() == (
            f"#{claim_id} · Umbrella / оркестратор · стенд · 2 ядра · 6 ГБ · идёт 0 мин из 30"
        )
        assert lines[2] == "заявлено: 2 ядра, 6 ГБ"

        result = _hub(core, "load", "done", str(claim_id))
        assert result.returncode == 0 and result.stdout.strip() == "ok"
        assert _claims(core) == []
        # снять второй раз — тоже успех
        assert _hub(core, "load", "done", str(claim_id)).returncode == 0
    finally:
        sleeper.kill()
        sleeper.wait(timeout=5)


def test_add_claim_dies_with_its_process(core):
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])  # noqa: S603
    try:
        argv = ["--what", "стенд", "--cores", "1", "--mem-gb", "0", "--minutes", "30"]
        assert _hub(core, "load", "add", "--pid", str(sleeper.pid), *argv).returncode == 0
        assert len(_claims(core)) == 1
    finally:
        sleeper.kill()
        sleeper.wait(timeout=5)
    assert _claims(core) == []
    assert _hub(core, "load").stdout.strip().splitlines()[-1] == "заявлено: ничего"


def test_add_dead_pid_refused(core):
    gone = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603
    gone.wait(timeout=10)
    argv = ["--what", "стенд", "--cores", "1", "--mem-gb", "0", "--minutes", "30"]
    result = _hub(core, "load", "add", "--pid", str(gone.pid), *argv)
    assert result.returncode != 0
    assert "hub load add" in result.stderr and str(gone.pid) in result.stderr
    assert _claims(core) == []


# --- второй вход `hub-load` -------------------------------------------------------------------------------

_HUB_LOAD = [sys.executable, "-c", "from hub.cli import load_main; load_main()"]


def _hub_load(env, *argv, timeout=30) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 — фиксированный локальный интерпретатор
        [*_HUB_LOAD, *argv], env=env, capture_output=True, text=True, timeout=timeout
    )


def test_hub_load_entry_point_is_declared():
    import tomllib

    scripts = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts["hub-load"] == "hub.cli:load_main"


def test_hub_load_entry_equals_hub_load(core):
    """`hub-load` = `hub load …`: показать, `--json`, `run` (запись на время команды), `add`, `done`."""
    shown = _hub_load(core)
    assert shown.returncode == 0, shown.stderr
    assert shown.stdout.splitlines()[1:] == _hub(core, "load").stdout.splitlines()[1:]  # 1-я строка — замер
    assert json.loads(_hub_load(core, "--json").stdout)["claims"] == []
    inner = (
        "import subprocess, sys; "
        f"r = subprocess.run({_HUB_LOAD!r} + ['--json'], capture_output=True, text=True); "
        "sys.stdout.write(r.stdout)"
    )
    result = _hub_load(core, "run", *_CLAIM, "--", sys.executable, "-c", inner)
    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)["claims"]) == 1
    assert _claims(core) == []
    failed = _hub_load(core, "run", *_CLAIM, "--", sys.executable, "-c", "import sys; sys.exit(7)")
    assert failed.returncode == 7 and _claims(core) == []
    added = _hub_load(core, "add", "--pid", str(os.getpid()), *_CLAIM)
    assert added.returncode == 0, added.stderr
    (claim,) = _claims(core)
    assert _hub_load(core, "done", str(claim["id"])).returncode == 0
    assert _claims(core) == []
