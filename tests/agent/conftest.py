"""Обвязка тестов дежурного агента (спека 7b, тесты 7–17; Ф57-1 — модели нет). Ядро — в процессе на временных
каталогах (`create_public_app` через `ASGITransport`, как `tests/hub_mcp/`); единственный транспорт агента —
это ядро, сеть не вызывается никогда. Режим бесед — настоящий, по правилам ядра. Часы — одни на ядро и агента
(`FakeClock`, секунды), уведомления — в список (`notes`), настоящий `notify-send` не зовётся.
"""

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from hub.agent import files
from hub.agent import main as agent_main
from hub.core import ingest, security
from hub.core.app_public import create_public_app

_API_CONFTEST_PATH = Path(__file__).resolve().parents[1] / "api" / "conftest.py"
_spec = importlib.util.spec_from_file_location("_hub_test_api_conftest_agent", _API_CONFTEST_PATH)
assert _spec is not None and _spec.loader is not None
_api_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_api_conftest)

FakeClock = _api_conftest.FakeClock
insert_project = _api_conftest.insert_project

PORT = 8787
CORE_URL = f"http://127.0.0.1:{PORT}/api/v1"


class Env:
    def __init__(self, db, clock, tmp_path: Path) -> None:
        self.db = db
        self.clock = clock
        self.app = create_public_app(db, clock=clock, port=PORT)
        self.config_dir = tmp_path / "config"
        self.data_dir = tmp_path / "data"
        self.config_dir.mkdir(mode=0o700)
        self.data_dir.mkdir(mode=0o700)
        self.notes: list[tuple[str, str, str]] = []
        self.agents: list = []
        self.account_id = db.write_sync(
            lambda conn: (
                conn.execute("INSERT INTO main.account (source, db) VALUES ('telegram', 'main')").lastrowid
            )
        )
        self.slack_account_id = db.write_sync(
            lambda conn: (
                conn.execute("INSERT INTO main.account (source, db) VALUES ('slack', 'work')").lastrowid
            )
        )
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
        self.token = created.token
        self.token_id = created.token_id
        files.write_secret(files.token_path(self.config_dir), self.token)
        self.write_config()

    def write_config(self, **overrides) -> None:
        config = dict(files.DEFAULT_CONFIG)
        config.update({"quiet_hours": ""})
        config.update(overrides)
        files.private_dir(files.config_agent_dir(self.config_dir))
        files.atomic_write(files.config_path(self.config_dir), json.dumps(config))

    def agent(self, **overrides):
        if overrides:
            self.write_config(**overrides)
        agent = agent_main.build(
            config_dir=self.config_dir,
            data_dir=self.data_dir,
            base_url=CORE_URL,
            core_transport=httpx.ASGITransport(app=self.app),
            notifier=lambda title, text, urgency: self.notes.append((title, text, urgency)),
            wall=self.clock,
            mono=self.clock,
        )
        self.agents.append(agent)
        return agent

    @property
    def now_ms(self) -> int:
        return int(self.clock() * 1000)

    def ingest(self, events: list[dict], *, account_id: int | None = None) -> None:
        acc = account_id if account_id is not None else self.account_id
        now_ms = self.now_ms
        result = self.db.write_sync(
            lambda conn: ingest.apply_batch(conn, account_id=acc, events=events, cursor=None, now_ms=now_ms)
        )
        assert result.dead == 0, result

    def channel_id(self, ext_id: str, schema: str = "main") -> int:
        with self.db.reader() as conn:
            return conn.execute(
                f"SELECT id FROM {schema}.channel WHERE ext_id = ?",  # noqa: S608
                (ext_id,),
            ).fetchone()[0]

    def message_id(self, chat: str, ext_id: str, schema: str = "main") -> int:
        with self.db.reader() as conn:
            return conn.execute(
                f"SELECT m.id FROM {schema}.message m JOIN {schema}.channel c ON c.id = m.channel_id "  # noqa: S608
                "WHERE c.ext_id = ? AND m.ext_id = ?",
                (chat, ext_id),
            ).fetchone()[0]

    def items(self) -> list[dict]:
        rows = []
        with self.db.reader() as conn:
            for schema in ("main", "work"):
                rows += conn.execute(
                    f"SELECT id, channel_id, label, summary, action, message_ids_json, status, provider "  # noqa: S608
                    f"FROM {schema}.agent_item ORDER BY id"
                ).fetchall()
        keys = ("id", "channel_id", "label", "summary", "action", "message_ids", "status", "provider")
        result = [dict(zip(keys, r, strict=True)) for r in rows]
        for item in result:
            item["message_ids"] = json.loads(item["message_ids"])
        return result

    def advance(self, seconds: float) -> None:
        self.clock.advance_ms(int(seconds * 1000))


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
async def env(db, clock, tmp_path):
    environment = Env(db, clock, tmp_path)
    yield environment
    for agent in environment.agents:
        await agent.core.aclose()


def msg(
    ext_id: str,
    ts: int,
    *,
    chat: str = "100",
    kind: str = "dm",
    title: str = "Пётр",
    text: str = "Когда релиз?",
    is_me: bool = False,
    mentions_me: bool = False,
    thread: str | None = None,
    mkind: str = "text",
    sender: str = "7",
    bulk: bool = False,
) -> dict:
    event = {
        "type": "upsert",
        "channel": {"ext_id": chat, "kind": kind, "title": title},
        "ext_id": ext_id,
        "ts": ts,
        "sender": {"ext_id": sender, "name": "Пётр Иванов"} if not is_me else {"ext_id": "1", "is_me": True},
        "is_me": is_me,
        "kind": mkind,
        "text": text,
        "mentions_me": mentions_me,
    }
    if thread is not None:
        event["thread_ext_id"] = thread
    if bulk:
        event["bulk"] = True  # Ф57-1 П1: письмо-рассылка (ставит коннектор почты)
    return event


def read_event(chat: str, up_to: str, *, kind: str = "dm") -> dict:
    return {"type": "read", "channel": {"ext_id": chat, "kind": kind}, "up_to_ext_id": up_to}
