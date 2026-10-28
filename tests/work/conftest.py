"""Обвязка тестов hub-work (сессии хаба). Ядро — в процессе на временных каталогах (`create_public_app` через
`ASGITransport`, как `tests/wake/`); `claude` — ПОДСТАВНОЙ исполняемый файл (`fake_claude.py`, stream-json по
сценарию): настоящий `claude` не запускается никогда. Уведомления — в список, настоящий `notify-send` не
зовётся. Действия владельца — через UI-сессию (cookie + CSRF), как в UI."""

import asyncio
import importlib.util
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from hub.core import security
from hub.core.app_public import create_public_app
from hub.work import files, free_sock
from hub.work import main as work_main

_API_CONFTEST_PATH = Path(__file__).resolve().parents[1] / "api" / "conftest.py"
_spec = importlib.util.spec_from_file_location("_hub_test_api_conftest_work", _API_CONFTEST_PATH)
assert _spec is not None and _spec.loader is not None
_api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_api)

PORT = 8787
CORE_URL = f"http://127.0.0.1:{PORT}/api/v1"
FAKE_SOURCE = Path(__file__).with_name("fake_claude.py")
GUARD_PATH = "/opt/fake/hub-work-guard"

FORBIDDEN_ENV = {
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
    "CLAUDECODE": "1",
    "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/sock",
    "CLAUDE_CONFIG_DIR": "/nonexistent/claude-config",
    "DISPLAY": ":0",
    "WAYLAND_DISPLAY": "wayland-0",
    "XAUTHORITY": "/run/user/1000/xauth",
}
KEPT_ENV = {
    "HOME": os.environ.get("HOME", "/tmp"),
    "PATH": "/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "SSH_AUTH_SOCK": "/run/user/1000/keyring/ssh",
    "DOCKER_HOST": "unix:///run/user/1000/docker.sock",
}


def install_fake_claude(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "claude"
    target.write_text(f"#!{sys.executable}\n" + FAKE_SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IXUSR)
    return target


def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
    )
    return proc.stdout.strip()


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main", cwd=path)
    (path / "app.py").write_text("print(1)\n")
    (path / ".gitignore").write_text("build/\n")
    git("add", ".", cwd=path)
    git("commit", "-qm", "init", cwd=path)
    return path


class Env:
    def __init__(self, db, tmp_path: Path) -> None:
        self.db = db
        self.tmp = tmp_path
        self.app = create_public_app(db, clock=time.time, port=PORT)
        self.config_dir = tmp_path / "config"
        self.data_dir = tmp_path / "data"
        self.home = tmp_path / "home"
        for d in (self.config_dir, self.data_dir, self.home):
            d.mkdir(mode=0o700)
        self.bin_dir = tmp_path / "bin"
        self.claude = install_fake_claude(self.bin_dir)
        self.notes: list[tuple[str, str, str]] = []
        self.hosts: list = []
        created = db.write_sync(
            lambda conn: security.create_api_token(
                conn,
                name=files.TOKEN_NAME,
                kind="background",
                scopes=list(files.TOKEN_SCOPES),
                project_ids=None,
                expires_ts=None,
            )
        )
        files.write_secret(files.token_path(self.config_dir), created.token)
        self.write_config()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url=f"http://127.0.0.1:{PORT}"
        )
        self._ui_headers: dict | None = None
        self._n = 0

    def write_config(self, **overrides) -> None:
        config = json.loads(json.dumps(files.DEFAULT_CONFIG))
        config.update(
            {
                "quiet_hours": "",
                "claude_path": str(self.claude),
                "idle_close_s": 3600,
                "batch_s": 0.05,
                "held_models_refresh_s": 0,  # Ф74-5б: чтение моделей Codex — только в своих тестах
            }
        )
        config.update(overrides)
        files.private_dir(files.config_work_dir(self.config_dir))
        files.atomic_write(files.config_path(self.config_dir), json.dumps(config))

    def host(self, **overrides):
        if overrides:
            self.write_config(**overrides)
        host = work_main.build(
            config_dir=self.config_dir,
            data_dir=self.data_dir,
            base_url=CORE_URL,
            core_transport=httpx.ASGITransport(app=self.app),
            notifier=lambda title, text, urgency: self.notes.append((title, text, urgency)),
            environ={**KEPT_ENV, **FORBIDDEN_ENV},
            auth_checker=lambda path, env: (True, "вход claude.ai"),
            home=self.home,
            guard_path=GUARD_PATH,
        )
        self.hosts.append(host)
        return host

    async def started(self, **overrides):
        host = self.host(**overrides)
        await host.start()
        return host

    # --- ядро: проекты, действия владельца -------------------------------------------------------------------

    async def ui_headers(self) -> dict:
        if self._ui_headers is None:
            cookie, _csrf = await _api.login_via_code(self.client, self.db, time.time)
            self.client.cookies.clear()
            self._ui_headers = {"Cookie": f"hub_session={cookie}", **_api.csrf_headers(cookie)}
        return self._ui_headers

    def project(self, name: str, *, repo: bool = True) -> tuple[int, Path]:
        self._n += 1
        workdir = self.tmp / f"proj-{self._n}"
        if repo:
            make_repo(workdir)
        else:
            workdir.mkdir()
        pid = _api.insert_project(self.db, name=name, workdir=str(workdir))
        return pid, workdir

    async def ui(self, method: str, path: str, body=None) -> httpx.Response:
        return await self.client.request(method, "/api/v1" + path, json=body, headers=await self.ui_headers())

    # --- подставной claude ----------------------------------------------------------------------------------

    def scenario(self, **value) -> None:
        (self.bin_dir / "scenario.json").write_text(json.dumps(value), encoding="utf-8")

    def _jsonl(self, name: str) -> list[dict]:
        path = self.bin_dir / name
        if not path.exists():
            return []
        # Журнал пишет другой процесс: последняя строка без перевода строки ещё дописывается — её не читаем
        # (иначе редкий `JSONDecodeError` посреди записи длинной строки).
        text = path.read_text(encoding="utf-8")
        text = text[: text.rfind("\n") + 1]
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def calls(self) -> list[dict]:
        return self._jsonl("calls.jsonl")

    def stdin(self) -> list[dict]:
        return self._jsonl("stdin.jsonl")

    def responses(self) -> list[dict]:
        return self._jsonl("responses.jsonl")

    def ends(self) -> list[dict]:
        return self._jsonl("ends.jsonl")

    def children(self) -> list[int]:
        return [c["pid"] for c in self._jsonl("children.jsonl")]

    # --- цикл хоста -----------------------------------------------------------------------------------------

    async def cycle(self, host) -> None:
        await host.tick()
        data = await host.core.get("/work/host/wait", params=host.wait_params(0))
        await host.apply(data)
        await host.flush()

    async def until(self, host, predicate, *, limit_s: float = 15.0, what: str = "условие"):
        deadline = time.monotonic() + limit_s
        while True:
            await self.cycle(host)
            value = predicate()
            if asyncio.iscoroutine(value):
                value = await value
            if value:
                return value
            if time.monotonic() > deadline:
                raise AssertionError(f"не дождались: {what}")
            await asyncio.sleep(0.05)


def flag_value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # зомби — не жив
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            return f.read().rsplit(")", 1)[-1].split()[0] != "Z"
    except OSError:
        return False


@pytest.fixture
async def env(db, tmp_path):
    instance = Env(db, tmp_path)
    yield instance
    for host in instance.hosts:
        for runner in list(host.held.runners.values()):  # Ф41-2б: процессы постоянных сессий
            runner.reason = runner.reason or "dead"
            if runner.alive:
                await runner.kill()
        await host.core.aclose()
    await instance.client.aclose()
    for pid in instance.children():
        try:
            os.killpg(pid, 9)
        except OSError:
            pass


@pytest.fixture
def free_sets():
    """Ф50-3: настоящий сокет набора автопилота, как у хоста: `free_sets.sets[<id сессии>] = [ключи]`,
    аргументы хука — `free_sets.args(<id>)`."""
    sets: dict[int, list] = {}
    server = free_sock.FreeServer(lambda sid: sets.get(sid, []))
    server.sets = sets
    server.start()
    yield server
    server.close()
