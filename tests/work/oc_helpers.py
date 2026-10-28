"""Общие помощники тестов хоста на поддельном OpenCode (Ф16): `Oc` — бинарь, сценарий, журналы, `engine.json`;
`set_engine`; `started`. Поддельный сервер — `tests/engine/fake_opencode.py`. Настоящие OpenCode и `claude`
здесь не запускаются. (Вынесено из теста чата оркестратора 9c, удалённого в Ф57-2; фикстура `oc` — из теста
задач по «Го» на OpenCode, удалённого в Ф78-1.)"""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from hub.core import engine as engine_mod

FAKE_SOURCE = Path(__file__).resolve().parents[1] / "engine" / "fake_opencode.py"


class Oc:
    """Поддельный `opencode` для хоста: бинарь, сценарий, журналы, `engine.json` в каталоге конфига."""

    def __init__(self, env) -> None:
        self.env = env
        self.dir = env.tmp / "oc"
        self.dir.mkdir()
        self.binary = self.dir / "opencode"
        self.binary.write_text(
            f"#!{sys.executable}\n" + FAKE_SOURCE.read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.binary.chmod(self.binary.stat().st_mode | stat.S_IXUSR)
        path = engine_mod.setup_path(env.config_dir)
        path.write_text(
            json.dumps(
                {
                    "opencode": {
                        "path": str(self.binary),
                        "version": "1.18.31",
                        "path_extra": [],
                        "disable_mcp": ["playwright", "hub-all"],
                    }
                }
            ),
            encoding="utf-8",
        )

    def scenario(self, *turns, **extra) -> None:
        (self.dir / "scenario.json").write_text(
            json.dumps({"turns": list(turns), **extra}, ensure_ascii=False), encoding="utf-8"
        )

    def jsonl(self, name: str) -> list[dict]:
        path = self.dir / name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def json(self, name: str):
        return json.loads((self.dir / name).read_text(encoding="utf-8"))

    def prompts(self) -> list[dict]:
        return [c for c in self.jsonl("calls.jsonl") if c["path"].endswith("/prompt_async")]

    def created(self) -> list[dict]:
        return [c for c in self.jsonl("calls.jsonl") if c["method"] == "POST" and c["path"] == "/session"]

    def aborts(self) -> list[dict]:
        return [c for c in self.jsonl("calls.jsonl") if c["path"].endswith("/abort")]

    def replies(self) -> list[dict]:
        return self.jsonl("replies.jsonl")

    def server_alive(self) -> bool:
        if not (self.dir / "pids.json").exists():
            return False
        pid = self.json("pids.json")["server"]
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"


def set_engine(env, engine: str) -> None:
    env.db.write_sync(lambda conn: engine_mod._meta_set(conn, engine_mod.META_ENGINE, engine))


async def started(env, **overrides):
    return await env.started(oc_poll_s=0.2, **overrides)


@pytest.fixture
async def oc(env):
    """Поддельный OpenCode на тест; процессы сессий хаба гасит `env`, здесь — наблюдатели выхода хоста."""
    fake = Oc(env)
    yield fake
    for host in env.hosts:
        for task in list(host._tasks):  # noqa: SLF001 — дождаться наблюдателей выхода
            task.cancel()
