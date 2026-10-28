"""Обвязка тестов слоя движка (Ф16, 16-2). `opencode` — ПОДДЕЛЬНЫЙ исполняемый файл (`fake_opencode.py` с
шебангом текущего интерпретатора во временном каталоге): настоящий OpenCode в тестах не запускается никогда.
Каталог сценария и журналов поддельного сервера — `FAKE_OC_DIR` (передаётся в окружении `OcServer.start`)."""

import json
import stat
import sys
from pathlib import Path

import pytest

from hub.engine import oc_config
from hub.engine.oc_server import OcServer

FAKE_SOURCE = Path(__file__).with_name("fake_opencode.py")

# Окружение, которое НЕ должно дойти до сервера OpenCode (фильтр `oc_server.server_env`).
FORBIDDEN_ENV = {
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
    "CLAUDE_CODE_OAUTH_TOKEN": "oauth-test",
    "CLAUDE_CONFIG_DIR": "/nonexistent/claude-config",
    "CLAUDECODE": "1",
    "OPENCODE_PERMISSION": '{"*": "allow"}',
    "OPENCODE_CONFIG_CONTENT": '{"permission": {"*": "allow"}}',
    "OPENCODE_SERVER_PASSWORD": "чужой-пароль",
}


def install_fake_opencode(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "opencode"
    target.write_text(f"#!{sys.executable}\n" + FAKE_SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IXUSR)
    return target


class FakeOc:
    """Каталог поддельного сервера: сценарий, журналы, запуск `OcServer` с ним."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.dir = tmp_path / "fake-oc"
        self.dir.mkdir()
        self.binary = install_fake_opencode(tmp_path / "bin")
        self.workdir = tmp_path / "work"
        self.workdir.mkdir()
        self.config_path = tmp_path / "cfg" / "oc.json"
        self.servers: list[OcServer] = []

    def scenario(self, **data) -> None:
        (self.dir / "scenario.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def env(self) -> dict:
        return {"PATH": "/usr/bin:/bin", "HOME": str(self.tmp), "FAKE_OC_DIR": str(self.dir), **FORBIDDEN_ENV}

    def config(self) -> dict:
        return oc_config.build("work", agent_name="hub-test", permission=oc_config.PERM_ASK_ALL)

    async def start(self, *, ready_timeout_s: float = 10.0) -> OcServer:
        oc_config.write(self.config_path, self.config())
        server = OcServer(str(self.binary), log_file=self.tmp / "logs" / "test-1.log")
        self.servers.append(server)
        await server.start(str(self.workdir), self.env(), self.config_path, ready_timeout_s=ready_timeout_s)
        return server

    def jsonl(self, name: str) -> list[dict]:
        path = self.dir / name
        if not path.exists():
            return []
        # Журнал пишет другой процесс: последняя строка без перевода строки ещё дописывается — её не читаем
        # (иначе редкий `JSONDecodeError` посреди записи длинной строки).
        text = path.read_text(encoding="utf-8")
        text = text[: text.rfind("\n") + 1]
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def json(self, name: str):
        return json.loads((self.dir / name).read_text(encoding="utf-8"))


@pytest.fixture
async def fake_oc(tmp_path):
    fake = FakeOc(tmp_path)
    yield fake
    for server in fake.servers:
        await server.stop(grace_s=1)
