"""Ф57-1 (docs/architecture.md), сквозные тесты «ядро + разборщик»: модели нет,
карточка по правилам — единственный путь.

1. сообщение человека в беседе `react` -> карточка `rules` и сигнал подписке `cards`;
2. письмо-рассылка в `react`-почте -> ни карточки, ни уведомления, ни сигнала, `unread_react` не растёт;
3. беседа без проекта: чат — тихо, упоминание в нём — карточка; почта от человека (и через группу) — карточка;
4. подписка проекта, слышащая «Неразобранное» (Ф52-2), получает сигнал по письму человека без проекта;
5. П6: открытая карточка прежней модели получает пересказ по правилам;
6. конфиг и состояние времён модели читаются, их ключи выбрасываются; команд модели в CLI нет.

Обвязка — `tests/agent/conftest.py`; подписка — настоящими ручками ядра, `/proc` подменён (как
`tests/api/test_f26_multi_subs.py`).
"""

import json
from pathlib import Path

import httpx
import pytest

from hub import cli as hub_cli
from hub.agent import dispatcher, files
from hub.agent import main as agent_main
from hub.core import security, subs, unsorted

from .conftest import CORE_URL, insert_project, msg, read_event

ORIGIN = "http://127.0.0.1:8787"
SESSION = (4242, 777_000, "sess-f57aaaa-1111")
_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def procs(monkeypatch):
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: {SESSION[0]: SESSION[1]}.get(pid))


@pytest.fixture
async def client(env):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url=ORIGIN) as c:
        yield c


def _project(env, tmp_path, name: str = "Umbrella") -> tuple[int, str]:
    workdir = tmp_path / f"wd-{name}"
    workdir.mkdir()
    pid = insert_project(env.db, name=name, workdir=str(workdir))
    env.db.write_sync(lambda conn: subs.set_project_subs(conn, pid, True, now_ms=0))
    return pid, str(workdir)


def _token(env, scopes=("read", "draft")) -> str:
    return env.db.write_sync(
        lambda conn: security.create_api_token(
            conn, name="mcp:all", kind="interactive", scopes=list(scopes), project_ids=None, expires_ts=None
        )
    ).token


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN, "X-Hub-Sub-Wait": "3"}


async def _subscribe(client, token: str, workdir: str, **extra) -> int:
    pid, start, sid = SESSION
    session = {
        "pid": pid,
        "proc_start": start,
        "session_id": sid,
        "cwd": workdir,
        "kind": "interactive",
        "name": None,
        "name_source": None,
    }
    r = await client.post("/api/v1/subs", json={"session": session, **extra}, headers=_headers(token))
    assert r.status_code == 200, r.text
    return r.json()["sub"]["id"]


async def _wait(client, token: str, sub_id: int) -> list[dict]:
    pid, start, sid = SESSION
    r = await client.post(
        f"/api/v1/subs/{sub_id}/wait",
        json={
            "session": {"pid": pid, "proc_start": start, "session_id": sid},
            "waiter": "0123456789abcdef",
            "timeout_s": 1,
        },
        headers=_headers(token),
    )
    assert r.status_code == 200, r.text
    return r.json()["signals"]


def _card_signals(env) -> list[tuple]:
    with env.db.reader() as conn:
        return conn.execute("SELECT sub_id, item_id, status FROM main.session_signal ORDER BY id").fetchall()


def _move(env, chat: str, project_id: int | None) -> int:
    cid = env.channel_id(chat)
    env.db.write_sync(
        lambda conn: conn.execute("UPDATE main.channel SET project_id = ? WHERE id = ?", (project_id, cid))
    )
    return cid


def _history(env, chat: str, *, kind: str, project_id: int | None, title: str = "Беседа") -> int:
    """Беседа с одним старым прочитанным сообщением (до старта агента), в нужном проекте."""
    env.ingest([msg("0", env.now_ms - 7_200_000, chat=chat, kind=kind, title=title, text="давно")])
    env.ingest([read_event(chat, "0", kind=kind)])
    return _move(env, chat, project_id)


def _mail(ext: str, env, *, chat: str = "t1", bulk: bool = False, text: str = "Добрый день, вопрос") -> dict:
    return msg(ext, env.now_ms, chat=chat, kind="mail_thread", title="Тема", text=text, bulk=bulk)


async def _agent(env, **config):
    agent = env.agent(**config)
    await agent.start()
    return agent


async def _cycle(env, agent, seconds: float = 16) -> int:
    await agent.poll_once()
    env.advance(seconds)
    return await agent.tick()


async def _unread_react(agent, channel_id: int) -> int:
    return (await agent.core.get(f"/channels/{channel_id}"))["unread_react"]


# --- 1: человек в `react` -> карточка по правилам и сигнал подписке `cards` --------------------------------


async def test_human_message_in_react_gives_rules_card_and_cards_signal(env, client, tmp_path, procs):
    pid, workdir = _project(env, tmp_path)
    cid = _history(env, "d1", kind="dm", project_id=pid, title="Клиент")
    token = _token(env)
    sub = await _subscribe(client, token, workdir)
    agent = await _agent(env)
    env.ingest([msg("1", env.now_ms, chat="d1", title="Клиент", text="Когда пришлёте договор?")])
    assert await _cycle(env, agent) == 1
    (item,) = env.items()
    assert (item["provider"], item["label"], item["status"]) == ("rules", "reply", "new")
    assert item["summary"] == "Пётр Иванов: Когда пришлёте договор?" and item["action"] == "личное"
    assert env.notes == [("Хаб · Umbrella: Клиент", "Пётр Иванов: Когда пришлёте договор?", "critical")]
    assert _card_signals(env) == [(sub, item["id"], "pending")]
    (signal,) = await _wait(client, token, sub)
    assert (signal["item_id"], signal["channel_id"], signal["label"]) == (item["id"], cid, "reply")
    assert await _unread_react(agent, cid) == 1


# --- 2: рассылка в `react`-почте — тихо ---------------------------------------------------------------------


async def test_bulk_letter_in_react_mail_is_silent(env, client, tmp_path, procs):
    pid, workdir = _project(env, tmp_path)
    cid = _history(env, "t1", kind="mail_thread", project_id=pid, title="Тема")
    token = _token(env)
    await _subscribe(client, token, workdir)
    agent = await _agent(env)
    env.ingest([_mail("1", env, bulk=True, text="Скидки недели")])
    assert await _cycle(env, agent) == 0
    assert agent.state["counters"]["skipped_attention"] == 1
    assert env.items() == [] and env.notes == [] and _card_signals(env) == []
    channel = await agent.core.get(f"/channels/{cid}")
    assert channel["attention"] == "react" and channel["unread_react"] == 0 and channel["unread"] == 1

    # письмо человека в той же беседе — как обычно: уведомление, карточка, сигнал
    env.ingest([_mail("2", env)])
    assert await _cycle(env, agent) == 1
    (item,) = env.items()
    assert item["message_ids"] == [env.message_id("t1", "2")]  # рассылка в карточку не входит
    assert item["action"] == "беседа «реагирую»"
    assert len(env.notes) == 1 and len(_card_signals(env)) == 1
    assert await _unread_react(agent, cid) == 1


# --- 3: беседа без проекта ----------------------------------------------------------------------------------


async def test_unsorted_chat_quiet_mention_card_human_mail_card(env):
    gid = _history(env, "g1", kind="group", project_id=None, title="Чат продавцов")
    agent = await _agent(env)
    env.ingest([msg("1", env.now_ms, chat="g1", kind="group", title="Чат продавцов", text="всем привет")])
    assert await _cycle(env, agent) == 0
    assert env.items() == [] and env.notes == [] and agent.buffers == {}

    env.ingest(
        [msg("2", env.now_ms, chat="g1", kind="group", title="Чат продавцов", text="владелец?", mentions_me=True)]
    )
    assert await _cycle(env, agent) == 1
    (card,) = env.items()
    assert (card["channel_id"], card["action"], card["provider"]) == (gid, "упоминание", "rules")

    # почта без проекта от человека — карточка; рассылка — нет; человек через группу Google (не `bulk`) — да
    env.ingest([_mail("1", env, chat="t1"), _mail("1", env, chat="t2", bulk=True)])
    env.ingest([_mail("1", env, chat="t3")])
    assert await _cycle(env, agent) == 2
    by_channel = {item["channel_id"]: item for item in env.items()}
    assert set(by_channel) == {gid, env.channel_id("t1"), env.channel_id("t3")}
    assert by_channel[env.channel_id("t1")]["action"] == "беседа «реагирую»"
    assert [n[0] for n in env.notes].count("Хаб · Неразобранное: Тема") == 2


# --- 4: Ф52-2 — подписка проекта слышит «Неразобранное» аккаунта -------------------------------------------


async def test_project_hearing_unsorted_gets_signal_for_human_mail(env, client, tmp_path, procs):
    pid, workdir = _project(env, tmp_path, "Hooli")
    mail_acc = env.db.write_sync(
        lambda conn: conn.execute("INSERT INTO main.account (source, db) VALUES ('mail', 'main')").lastrowid
    )
    assert env.db.write_sync(lambda conn: unsorted.set_project(conn, pid, [mail_acc])) == [mail_acc]
    token = _token(env, scopes=("read", "draft", "organize"))  # сам видит «Неразобранное»
    sub = await _subscribe(client, token, workdir)
    agent = await _agent(env)
    env.ingest([_mail("1", env, chat="t1", bulk=True)], account_id=mail_acc)
    assert await _cycle(env, agent) == 0
    assert _card_signals(env) == []

    env.ingest([_mail("1", env, chat="t2")], account_id=mail_acc)
    assert await _cycle(env, agent) == 1
    (item,) = env.items()
    assert _card_signals(env) == [(sub, item["id"], "pending")]
    (signal,) = await _wait(client, token, sub)
    assert signal["item_id"] == item["id"] and signal["unsorted"] is True
    assert signal["account_id"] == mail_acc and signal["channel_id"] == env.channel_id("t2")


# --- 5 (П6): открытая карточка прежней модели получает пересказ по правилам -------------------------------


async def test_open_model_card_gets_rules_summary_on_new_message(env):
    agent = await _agent(env)
    env.ingest([msg("1", env.now_ms - 1000, chat="100", text="первое")])
    await agent.poll_once()
    first = env.message_id("100", "1")
    old = await agent.core.post(
        "/agent/items",
        json={
            "channel_id": env.channel_id("100"),
            "label": "urgent",
            "summary": "Пересказ прежней модели, давно неверный.",
            "action": "Ответить.",
            "message_ids": [first],
            "provider": "ollama",
            "model": "qwen3.5:4b",
        },
    )
    env.ingest([msg("2", env.now_ms, chat="100", text="второе")])
    assert await _cycle(env, agent) == 1
    (item,) = env.items()
    assert item["id"] == old["id"] and item["provider"] == "rules"
    assert item["summary"] == "Пётр Иванов: второе" and item["action"] == "личное"
    assert item["label"] == "urgent"  # метка не понижается
    assert item["message_ids"] == [first, env.message_id("100", "2")]


# --- 6: конфиг, состояние, CLI без модели ------------------------------------------------------------------


async def test_legacy_model_config_and_state_keys_are_dropped(env):
    now = env.now_ms
    env.write_config(
        # раньше внешний адрес модели не давал стартовать — теперь ключ не читается
        llm_url="http://203.0.113.5:11434",
        llm_model="qwen3.5:4b",
        llm_num_ctx=8192,
        llm_keep_alive="1m",
        llm_timeout_s=180,
        llm_max_prompt_chars=14000,
        max_calls_per_hour=1,
        notify_labels=["urgent"],
        mature_s=90,
        mature_priority_s=15,
        mature_count=30,
        provider="deepseek",
        base_url="https://api.deepseek.com",
        daily_budget_usd=0.0,
    )
    files.save_state(
        env.data_dir,
        {
            "seq": 0,
            "calls": [now - 1000] * 5,
            "stopped": {"ts": now, "reason": "модель не скачана", "model": "qwen3.5:4b"},
            "last_llm_ok_ts": now,
            "calls_notified_ts": now,
            "day": "2026-09-24",
            "spent_usd": 1e6,
            "counters": {"triaged": 3},
        },
    )
    agent = await _agent(env)
    assert not set(files.LEGACY_CONFIG_KEYS) & set(agent.config)
    assert not [key for key in agent.config if key.startswith(("llm_", "mature_"))]
    assert agent.config["react_silence_s"] == 15 and agent.config["skip_channel_titles"] == ["#youtrack"]
    assert not set(dispatcher.LEGACY_STATE_KEYS) & set(agent.state)
    stored = files.load_state(env.data_dir)
    assert not set(dispatcher.LEGACY_STATE_KEYS) & set(stored)
    assert stored["counters"] == {"triaged": 3}  # прочее состояние цело
    # «лимит вызовов исчерпан» и «разбор остановлен» больше ничего не держат
    env.ingest([msg("1", env.now_ms - 1000, chat="4000")])
    assert await _cycle(env, agent) == 1
    assert not hasattr(agent, "provider")
    text = agent_main.status_text(env.data_dir, max_seq=agent.sync_pos, now_ms=env.now_ms)
    assert "LLM" not in text and "ОСТАНОВЛЕН" not in text and "курсор:" in text
    assert not [key for key in files.DEFAULT_CONFIG if key.startswith(("llm_", "mature_"))]
    assert "max_calls_per_hour" not in files.DEFAULT_CONFIG and "notify_labels" not in files.DEFAULT_CONFIG


def test_agent_builds_with_core_transport_only():
    """Единственный адрес разборщика — ядро: в пакете нет ни провайдера модели, ни её адреса."""
    agent_dir = _ROOT / "src" / "hub" / "agent"
    assert sorted(p.name for p in agent_dir.glob("*.py")) == [
        "__init__.py",
        "core_api.py",
        "dispatcher.py",
        "files.py",
        "main.py",
    ]
    source = "\n".join(p.read_text(encoding="utf-8") for p in agent_dir.glob("*.py"))
    for needle in ("11434", "/api/chat", "api.deepseek.com", "OllamaProvider", "ProviderError"):
        assert needle not in source, needle
    for name in ("check_llm", "DEEPSEEK_REFUSAL", "EXIT_NOT_LOCAL"):
        assert not hasattr(agent_main, name), name
    for name in ("_triage", "_fail", "_ask", "_block_reason", "calls_last_hour", "_notify_b7", "_service"):
        assert not hasattr(dispatcher.Agent, name), name
    assert CORE_URL.startswith("http://127.0.0.1:")


def test_cli_has_no_model_commands(tmp_path, monkeypatch, capsys):
    parser = hub_cli._build_parser()  # noqa: SLF001
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["agent", "check"])
    assert exc_info.value.code == 2
    for command in ("setup", "run", "status"):
        assert parser.parse_args(["agent", command]).agent_command == command
    capsys.readouterr()
    # `hub auth deepseek` — больше не особый случай: обычный ответ про неизвестный источник, без записи
    config_dir, data_dir = tmp_path / "config", tmp_path / "data"
    monkeypatch.setenv("HUB_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("HUB_DATA_DIR", str(data_dir))
    hub_cli.main(["auth", "deepseek"])
    out = capsys.readouterr().out
    assert "DeepSeek отключён" not in out and "Ollama" not in out
    assert not config_dir.exists() and not data_dir.exists()


def test_unit_install_readme_texts_without_model():
    unit = (_ROOT / "deploy" / "systemd" / "hub-agent.service").read_text(encoding="utf-8")
    assert "hub agent check" not in unit and "ollama" not in unit.lower()
    # не сужается: ядро может быть на ::1
    assert "\nRestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\n" in unit
    install = (_ROOT / "scripts" / "hub-units-install.sh").read_text(encoding="utf-8")
    assert "hub agent check" not in install and "hub agent setup" in install
    assert json.loads(json.dumps(files.DEFAULT_CONFIG))["react_max_wait_s"] == 120
