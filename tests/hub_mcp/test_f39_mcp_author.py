"""Ф39 B (docs/architecture.md, B2–B4 и поправка П7): MCP и автор черновика.

- B3: своё сообщение, положенное сессией с ролью, — «я (роль)» в `hub_read`, `hub_message`, `hub_search`,
  `hub_find_channel`; без роли — «я»; `hub_drafts` — «роль «…»»; описание `hub_read`;
- B4 + П7: строка-подсказка в конце ответа `hub_draft` по `heard` ядра (`no`, `cards`; остальное — без
  строки), и когда ушло сразу, и с файлами;
- B2: `hub_draft_new` передаёт ядру сессию (и из OpenCode — `oc_session`/`oc_dir`).

Настоящее ядро — в процессе; подменное — `httpx.MockTransport` (обвязка — `test_f37_mcp_autosend.py`)."""

import os

import pytest
from mcp import Client

from hub import hostid
from hub import session_registry as reg
from hub.core import subs

from .conftest import (
    insert_account,
    insert_channel,
    insert_message,
    insert_project,
    make_token,
    text_of,
    write_profile,
)
from .test_f37_mcp_autosend import FILE_ROUTES, ME, OC_A, ROLE, _call, _mock_server, _send_answer

# Ф33-1: тело `session` от найденной сессии несёт машину (`hub.hostid`).
HOST = {"host": hostid.host_id({})} if hostid.host_id({}) else {}
HOST |= {"boot": hostid.boot_id({})} if hostid.boot_id({}) else {}  # Ф33-1: загрузка машины


NO_HINT = (
    "Эту беседу твоя сессия не слушает — ответ сюда сам не придёт. Следить: hub_subscribe(channel_id={})."
)
CARDS_HINT = (
    "Подписка на проект в режиме карточек услышит ответ, только если дежурный заведёт карточку; надёжнее — "
    "hub_subscribe(channel_id={})."
)


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
    insert_message(db, channel, ext_id="100", ts=int(clock() * 1000) - 60_000, text="вопрос команде")
    token, token_id = make_token(db, scopes=("read", "draft"), name="mcp:all")
    write_profile(config_dir, "all", token, token_id)
    server, hub = make_server("all")
    hub.environ = {}
    hub.getppid = lambda: 1
    hub.find_session = lambda start: me
    return {"server": server, "db": db, "channel": channel, "project": project, "account": account}


def _authors(db) -> list[tuple]:
    with db.reader() as conn:
        return conn.execute("SELECT id, author_role, author_sub_id FROM main.outbox ORDER BY id").fetchall()


# --- настоящее ядро -----------------------------------------------------------------------------------------


async def test_role_of_author_in_reads_real_core(stand):
    server, db, channel = stand["server"], stand["db"], stand["channel"]
    # Без подписки: автора нет — «я», и подсказка «подпишись» (беседу сессия не слушает).
    text = text_of(await _call(server, "hub_draft", channel_id=channel, text="первый черновик альфа"))
    assert text.endswith("\n" + NO_HINT.format(channel))
    result = await _call(server, "hub_subscribe", role=ROLE)
    assert not result.is_error, text_of(result)
    # Подписка на проект в режиме карточек — своя подсказка.
    text = text_of(await _call(server, "hub_draft", channel_id=channel, text="второй черновик бета"))
    assert "(статус pending_approval)" in text and text.endswith("\n" + CARDS_HINT.format(channel))
    # Подписка на саму беседу — без подсказки.
    result = await _call(server, "hub_subscribe", channel_id=channel)
    assert not result.is_error, text_of(result)
    text = text_of(await _call(server, "hub_draft", channel_id=channel, text="третий черновик гамма"))
    assert "hub_subscribe" not in text and text.endswith("Ничего не отправлено.")
    rows = _authors(db)
    assert [(r[1], r[2] is not None) for r in rows] == [(None, False), (ROLE, True), (ROLE, True)]

    read = text_of(await _call(server, "hub_read", channel_id=channel))
    mark = "[моя заготовка: ждёт подтверждения владельца]"
    assert f"] я: {mark} первый черновик альфа" in read
    assert f"] я ({ROLE}): {mark} второй черновик бета" in read
    assert f"] я ({ROLE}): {mark} третий черновик гамма" in read
    message_id = int(read.split(f"] я ({ROLE}): {mark} второй")[0].rsplit("[msg ", 1)[1].split(" ", 1)[0])
    one = text_of(await _call(server, "hub_message", message_id=message_id))
    assert f"] я ({ROLE}): {mark} второй черновик бета" in one

    found = text_of(await _call(server, "hub_search", query="бета"))
    assert f"] я ({ROLE}): " in found
    found = text_of(await _call(server, "hub_search", query="альфа"))
    assert "] я: " in found and f"я ({ROLE})" not in found
    resolved = text_of(await _call(server, "hub_find_channel", query="Команда"))
    assert f"] я ({ROLE}): третий черновик гамма" in resolved and "] я: первый черновик альфа" in resolved

    drafts = text_of(await _call(server, "hub_drafts"))
    lines = [line for line in drafts.splitlines() if line.startswith("#")]
    assert len(lines) == 3
    assert [f"· агент/клиент API · роль «{ROLE}» · мой" in line for line in lines].count(True) == 2
    assert [line for line in lines if "роль «" not in line and "· агент/клиент API · мой" in line]


async def test_draft_new_writes_author_real_core(stand):
    server, db = stand["server"], stand["db"]
    result = await _call(server, "hub_subscribe", role=ROLE)
    assert not result.is_error, text_of(result)
    result = await _call(
        server,
        "hub_draft_new",
        project_id=stand["project"],
        account_id=stand["account"],
        to="@petr_ivanov",
        text="Здравствуйте!",
    )
    assert not result.is_error, text_of(result)
    assert "hub_subscribe" not in text_of(result)  # беседа-заготовка: подсказки «подпишись» нет
    [(_id, role, sub_id)] = _authors(db)
    assert role == ROLE and sub_id is not None


async def test_read_tool_description_explains_role(stand):
    async with Client(stand["server"]) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert "«я (роль)» — сообщение положила сессия с этой ролью" in tools["hub_read"].description
    assert "старше 05.10.2026" in tools["hub_read"].description
    props = tools["hub_draft_new"].input_schema["properties"]
    assert "заполняет плагин хаба" in props["oc_dir"]["description"]


# --- подменное ядро: подсказка и тело запроса ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("heard", "hint"),
    [
        ("no", NO_HINT),
        ("cards", CARDS_HINT),
        ("channel", None),
        ("project", None),
        ("personal", None),
        (None, None),  # ядро старше Ф39 или запрос без сессии — поля нет
        ("что-то новое", None),
    ],
)
async def test_heard_hint_texts(config_dir, make_server, heard, hint):
    answer = _send_answer() if heard is None else _send_answer(heard=heard)
    server, _hub, _core = _mock_server(make_server, config_dir, answer, me=ME)
    text = text_of(await _call(server, "hub_draft", channel_id=283, text="ок"))
    assert text.startswith("Черновик #7 (статус pending_approval)")
    if hint is None:
        assert "hub_subscribe" not in text
    else:
        assert text.endswith("Ничего не отправлено.\n" + hint.format(283))


async def test_heard_hint_when_sent_at_once_and_with_files(config_dir, make_server):
    answer = _send_answer(status="queued", auto={"role": ROLE}, heard="no")
    server, _hub, _core = _mock_server(make_server, config_dir, answer, me=ME)
    text = text_of(await _call(server, "hub_draft", channel_id=283, text="ок"))
    assert text.startswith("Отправлено сразу, без подтверждения владельца (#7)")
    assert text.endswith("Отменить нельзя.\n" + NO_HINT.format(283))

    _server, hub, _core = _mock_server(
        make_server, config_dir, _send_answer(heard="cards"), me=ME, routes=FILE_ROUTES
    )
    text = await hub.draft(283, "подпись", None, None, files=["media:5"])
    assert "Черновик #7 с файлами (1) ждёт подтверждения владельца" in text
    assert text.endswith("\n" + CARDS_HINT.format(283))


def _new_routes() -> dict:
    return {
        ("POST", "/drafts/new"): (
            200,
            {
                "channel_id": 9,
                "outbox_id": 7,
                "message_id": 70,
                "status": "pending_approval",
                "existing": False,
            },
        )
    }


def _new_body(core) -> dict:
    [body] = [b for method, path, b in core.seen if (method, path) == ("POST", "/drafts/new")]
    return body


async def test_draft_new_carries_session(config_dir, make_server):
    server, _hub, core = _mock_server(make_server, config_dir, _send_answer(), me=ME, routes=_new_routes())
    result = await _call(
        server, "hub_draft_new", project_id=3, account_id=1, to="@petr_ivanov", text="Здравствуйте!"
    )
    assert not result.is_error, text_of(result)
    assert _new_body(core)["session"] == ME.session()
    # Дежурная, ошибка реестра: поля нет, черновик кладётся как раньше.
    server, _hub, core = _mock_server(make_server, config_dir, _send_answer(), routes=_new_routes())
    result = await _call(
        server, "hub_draft_new", project_id=3, account_id=1, to="@petr_ivanov", text="Здравствуйте!"
    )
    assert not result.is_error, text_of(result)
    assert "session" not in _new_body(core)


async def test_draft_new_opencode_session_from_plugin_args(config_dir, make_server, tmp_path):
    env = {"OPENCODE_PID": str(os.getpid()), "OPENCODE": "1"}
    server, hub, core = _mock_server(
        make_server, config_dir, _send_answer(), environ=env, routes=_new_routes()
    )
    hub.getppid = os.getpid
    result = await _call(
        server,
        "hub_draft_new",
        project_id=3,
        account_id=1,
        to="@petr_ivanov",
        text="из OpenCode",
        oc_session=OC_A,
        oc_dir=str(tmp_path),
    )
    assert not result.is_error, text_of(result)
    assert _new_body(core)["session"] == {
        "pid": os.getpid(),
        "proc_start": subs.read_proc_start(os.getpid()),
        "session_id": OC_A,
        **HOST,
    }
