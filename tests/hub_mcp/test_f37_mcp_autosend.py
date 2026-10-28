"""Ф37-1 (docs/architecture.md, B5, тест E7 и поправки П4, П5): MCP `hub_draft` передаёт ядру
сессию и говорит сессии прямо, что ушло сразу.

- три текста: «Отправлено сразу…», «Лимит … исчерпан — лёг черновиком», прежний;
- `session` в теле `POST /messages/send` — через `_me()` (без отказа): у дежурной и рабочей сессии, при
  ошибке реестра поля нет, черновик кладётся как раньше (П4); с файлами — так же;
- OpenCode: служебные `oc_session`/`oc_dir` у `hub_draft` (с Ф39 B2 — и у `hub_draft_new`: автор черновика).

Настоящее ядро — в процессе (подписка, список владельца, строка очереди); подменное — `httpx.MockTransport`."""

import json
import os

import httpx
import pytest
from mcp import Client

from hub import hostid
from hub import session_registry as reg
from hub.core import autosend, subs

from .conftest import (
    insert_account,
    insert_channel,
    insert_message,
    insert_project,
    make_token,
    text_of,
    write_profile,
)

# Ф33-1: тело `session` от найденной сессии несёт машину (`hub.hostid`).
HOST = {"host": hostid.host_id({})} if hostid.host_id({}) else {}
HOST |= {"boot": hostid.boot_id({})} if hostid.boot_id({}) else {}  # Ф33-1: загрузка машины


ROLE = "маркетолог"
OC_A = "ses_AAAAAAAAAA1"


async def _call(server, name, **args):
    async with Client(server) as client:
        return await client.call_tool(name, args)


class Core:
    """Подменное ядро: `(метод, путь) -> (статус, тело)`; помнит запросы."""

    def __init__(self, routes):
        self.routes = routes
        self.seen: list[tuple[str, str, dict | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        is_json = request.headers.get("content-type", "").startswith("application/json")
        body = json.loads(request.content) if request.content and is_json else None
        path = request.url.path.removeprefix("/api/v1")
        self.seen.append((request.method, path, body))
        status, payload = self.routes[(request.method, path)]
        return httpx.Response(status, json=payload)

    def sent(self) -> dict:
        [body] = [b for method, path, b in self.seen if (method, path) == ("POST", "/messages/send")]
        return body


def _send_answer(**extra) -> dict:
    answer = {
        "outbox_id": 7,
        "message_id": 70,
        "status": "pending_approval",
        "created": True,
        "paced": False,
        "send_at_ts": None,
        "cancel_if_reply": False,
    }
    return {**answer, **extra}


def _mock_server(make_server, config_dir, answer, *, me=None, environ=None, routes=None):
    core = Core({("POST", "/messages/send"): (200, answer), **(routes or {})})
    write_profile(config_dir, "all", "hubt_x")
    server, hub = make_server("all", transport=httpx.MockTransport(core.handler))
    hub.environ = {} if environ is None else environ
    hub.getppid = lambda: 1
    if me is None:

        def gone(_start):
            raise reg.RegistryError(reg.GONE, "процесса сессии Claude Code среди предков нет")

        hub.find_session = gone
    else:
        hub.find_session = lambda _start: me
    return server, hub, core


ME = reg.SessionInfo(4242, 777_000, "sess-aaaaaaaa-1111", "/tmp/proj", None, None, "interactive")


# --- настоящее ядро: ушло сразу и обычный черновик ---------------------------------------------------------


@pytest.fixture
def stand(db, clock, config_dir, make_server, tmp_path, monkeypatch):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    me = reg.SessionInfo(4242, 777_000, "sess-aaaaaaaa-1111", str(workdir), None, None, "interactive")
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: {me.pid: me.proc_start}.get(pid))
    project = insert_project(db, name="Umbrella", workdir=str(workdir))
    db.write_sync(lambda conn: subs.set_project_subs(conn, project, True, now_ms=0))
    account = insert_account(db, source="telegram")
    channel = insert_channel(db, account, project_id=project, ext_id="team", kind="group", title="Команда")
    other = insert_channel(db, account, project_id=project, ext_id="other", kind="group", title="Другая")
    insert_message(db, channel, ext_id="100", ts=int(clock() * 1000) - 60_000)
    token, token_id = make_token(db, scopes=("read", "draft"), name="mcp:all")
    write_profile(config_dir, "all", token, token_id)
    server, hub = make_server("all")
    hub.environ = {}
    hub.getppid = lambda: 1
    hub.find_session = lambda start: me
    db.write_sync(lambda conn: autosend.set_role(conn, channel, role=ROLE, on=True, now_ms=1))
    return {"server": server, "db": db, "channel": channel, "other": other}


def _outbox(db) -> list[tuple]:
    with db.reader() as conn:
        return conn.execute(
            "SELECT id, channel_id, status, auto_role FROM main.outbox ORDER BY id"
        ).fetchall()


async def test_draft_sent_at_once_real_core(stand):
    server, db, channel, other = stand["server"], stand["db"], stand["channel"], stand["other"]
    # Без подписки — обычный черновик, прежний текст.
    text = text_of(await _call(server, "hub_draft", channel_id=channel, text="Привет, команда"))
    assert "(статус pending_approval): черновик ждёт подтверждения владельца" in text
    assert "Ничего не отправлено." in text and "Отправлено сразу" not in text

    result = await _call(server, "hub_subscribe", role=ROLE)
    assert not result.is_error, text_of(result)
    result = await _call(server, "hub_draft", channel_id=channel, text="Отвечаю команде")
    text = text_of(result)
    assert not result.is_error, text
    assert text.startswith("Отправлено сразу, без подтверждения владельца (#2): ")
    assert f"беседа #{channel} разрешена роли «{ROLE}». Отменить нельзя." in text
    assert "Ничего не отправлено" not in text and "ждёт подтверждения" not in text

    # Беседа не из списка — прежний текст.
    text = text_of(await _call(server, "hub_draft", channel_id=other, text="В другую беседу"))
    assert "(статус pending_approval): черновик ждёт подтверждения владельца" in text

    assert [(row[1], row[2], row[3]) for row in _outbox(db)] == [
        (channel, "pending_approval", None),
        (channel, "queued", ROLE),
        (other, "pending_approval", None),
    ]


async def test_draft_tool_description_mentions_autosend(stand):
    async with Client(stand["server"]) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    description = tools["hub_draft"].description
    assert "Исключение — беседа, которую владелец разрешил роли твоей подписки" in description
    props = tools["hub_draft"].input_schema["properties"]
    assert "заполняет плагин хаба" in props["oc_session"]["description"]
    assert "заполняет плагин хаба" in props["oc_dir"]["description"]
    # Ф39 B2: `hub_draft_new` тоже опознаёт сессию — по ней ядро записывает автора черновика.
    new_props = tools["hub_draft_new"].input_schema["properties"]
    assert "заполняет плагин хаба" in new_props["oc_session"]["description"]


# --- подменное ядро: тексты и тело запроса -----------------------------------------------------------------


async def test_texts_for_auto_limit_and_plain(config_dir, make_server):
    answer = _send_answer(status="queued", auto={"role": "Маркетолог"})
    server, _hub, core = _mock_server(make_server, config_dir, answer, me=ME)
    text = text_of(await _call(server, "hub_draft", channel_id=283, text="ок"))
    assert text == (
        "Отправлено сразу, без подтверждения владельца (#7): беседа #283 разрешена роли «Маркетолог». "
        "Отменить нельзя."
    )
    assert core.sent()["session"] == ME.session()


@pytest.mark.parametrize(
    ("limit_of", "words"),
    [("channel", "40 в час на беседу"), (None, "40 в час на беседу"), ("token", "60 в час на все беседы")],
)
async def test_text_for_limit(config_dir, make_server, limit_of, words):
    answer = _send_answer(auto_denied="limit", auto_limit=limit_of)
    server, _hub, _core = _mock_server(make_server, config_dir, answer, me=ME)
    text = text_of(await _call(server, "hub_draft", channel_id=283, text="ок"))
    assert text.startswith(f"Лимит отправок без подтверждения ({words}) исчерпан — лёг черновиком #7")
    assert "Ничего не отправлено." in text and "Отправлено сразу" not in text


async def test_queued_without_auto_keeps_old_text(config_dir, make_server):
    """Повтор по ключу уже ушедшей строки (`queued` без `auto`) — прежний нейтральный текст, не «отправлено
    сразу»."""
    server, _hub, _core = _mock_server(make_server, config_dir, _send_answer(status="queued"), me=ME)
    text = text_of(await _call(server, "hub_draft", channel_id=283, text="ок"))
    assert text == "Черновик #7: статус queued."


# --- П4: дежурная, рабочая сессия, ошибка реестра — без `session`, черновик как раньше ---------------------


async def test_p4_no_session_when_registry_fails(config_dir, make_server):
    """hub-wake (`claude -p`, в реестре сессий её нет) и любая ошибка реестра: `_me()` не отказывает."""
    server, _hub, core = _mock_server(make_server, config_dir, _send_answer())
    result = await _call(server, "hub_draft", channel_id=283, text="черновик дежурной")
    text = text_of(result)
    assert not result.is_error, text
    assert "(статус pending_approval): черновик ждёт подтверждения владельца" in text
    body = core.sent()
    assert "session" not in body
    assert set(body) == {"channel_id", "text", "reply_to", "thread", "idempotency_key"}


async def test_p4_no_session_in_work_session(config_dir, make_server):
    """hub-work (`HUB_WORK=1`): реестр не читается вовсе, поля `session` нет, отказа нет."""
    server, hub, core = _mock_server(
        make_server, config_dir, _send_answer(), me=ME, environ={reg.WORK_ENV: "1"}
    )

    def never(_start):
        raise AssertionError("рабочая сессия реестр не читает")

    hub.find_session = never
    result = await _call(server, "hub_draft", channel_id=283, text="черновик рабочей сессии")
    assert not result.is_error, text_of(result)
    assert "session" not in core.sent()


async def test_confirm_true_sends_no_session(config_dir, make_server):
    """`confirm=True` — сессия сама просит подтверждения владельца: `session` в ядро не уходит, значит и в беседе
    «без подтверждения» ляжет обычный черновик; без `confirm` та же сессия `session` передаёт."""
    server, _hub, core = _mock_server(make_server, config_dir, _send_answer(), me=ME)
    result = await _call(server, "hub_draft", channel_id=283, text="не уверен — спрошу владельца", confirm=True)
    assert not result.is_error, text_of(result)
    assert "session" not in core.sent()
    # Ф39 B2: автор такого черновика идёт отдельным полем — авто-отправка его не видит.
    assert core.sent()["author_session"] == {
        "pid": ME.pid,
        "proc_start": ME.proc_start,
        "session_id": ME.session_id,
    }
    server, _hub, core = _mock_server(make_server, config_dir, _send_answer(), me=ME)
    result = await _call(server, "hub_draft", channel_id=283, text="обычный ответ")
    assert not result.is_error, text_of(result)
    assert core.sent()["session"] == {"pid": ME.pid, "proc_start": ME.proc_start, "session_id": ME.session_id}
    assert "author_session" not in core.sent()


async def test_draft_with_files_carries_session(config_dir, make_server):
    routes = {
        ("GET", "/channels/283"): (200, {"id": 283, "project_id": 3, "source": "telegram"}),
        ("POST", "/uploads/from-media"): (200, {"upload_id": "u" * 32, "name": "a.pdf"}),
    }
    answer = _send_answer()
    server, hub, core = _mock_server(make_server, config_dir, answer, me=ME, routes=routes)
    text = await hub.draft(283, "подпись", None, None, files=["media:5"])
    assert "Черновик #7 с файлами (1) ждёт подтверждения владельца" in text
    body = core.sent()
    assert body["session"] == ME.session() and body["files"] == ["u" * 32]

    _server2, hub2, core2 = _mock_server(make_server, config_dir, answer, routes=routes)
    await hub2.draft(283, "подпись", None, None, files=["media:5"])
    assert "session" not in core2.sent()


FILE_ROUTES = {
    ("GET", "/channels/283"): (200, {"id": 283, "project_id": 3, "source": "telegram"}),
    ("POST", "/uploads/from-media"): (200, {"upload_id": "u" * 32, "name": "a.pdf"}),
}


async def test_files_sent_at_once_text(config_dir, make_server):
    """Владелец 04.10 (П6 снята): с файлами в разрешённой беседе уходит сразу — ответ говорит это прямо, а не
    «ждёт подтверждения»; лимит — свой текст; `confirm=True` — без `session`, обычный черновик."""
    answer = _send_answer(status="queued", auto={"role": ROLE})
    _server, hub, _core = _mock_server(make_server, config_dir, answer, me=ME, routes=FILE_ROUTES)
    text = await hub.draft(283, "подпись", None, None, files=["media:5"])
    assert "Отправлено сразу, без подтверждения владельца (#7), с файлами (1)" in text
    assert f"роли «{ROLE}»" in text and "ждёт подтверждения" not in text and "файл: a.pdf" in text

    answer = _send_answer(auto_denied="limit", auto_limit="channel")
    _server, hub, _core = _mock_server(make_server, config_dir, answer, me=ME, routes=FILE_ROUTES)
    text = await hub.draft(283, "подпись", None, None, files=["media:5"])
    assert "Лимит отправок без подтверждения (40 в час на беседу) исчерпан" in text
    assert "#7 с файлами (1)" in text and "Ничего не отправлено" in text

    _server, hub, core = _mock_server(make_server, config_dir, _send_answer(), me=ME, routes=FILE_ROUTES)
    text = await hub.draft(283, "подпись", None, None, files=["media:5"], confirm=True)
    assert "session" not in core.sent() and "Черновик #7 с файлами (1) ждёт подтверждения владельца" in text


# --- OpenCode: `oc_session`/`oc_dir` -----------------------------------------------------------------------


async def test_opencode_session_from_plugin_args(config_dir, make_server, tmp_path):
    env = {"OPENCODE_PID": str(os.getpid()), "OPENCODE": "1"}
    server, hub, core = _mock_server(make_server, config_dir, _send_answer(), environ=env)
    hub.getppid = os.getpid

    def no_claude(_start):
        raise AssertionError("реестр Claude Code в сессии OpenCode не читается")

    hub.find_session = no_claude
    result = await _call(
        server, "hub_draft", channel_id=283, text="из OpenCode", oc_session=OC_A, oc_dir=str(tmp_path)
    )
    assert not result.is_error, text_of(result)
    assert core.sent()["session"] == {
        "pid": os.getpid(),
        "proc_start": subs.read_proc_start(os.getpid()),
        "session_id": OC_A,
        **HOST,
    }

    # Плагина нет (служебные поля пусты) — сессия не определилась: черновик без `session`, без отказа.
    server, hub, core = _mock_server(make_server, config_dir, _send_answer(), environ=env)
    hub.getppid = os.getpid
    hub.find_session = no_claude
    result = await _call(server, "hub_draft", channel_id=283, text="без плагина")
    assert not result.is_error, text_of(result)
    assert "session" not in core.sent()
