"""Тест 9 (кусок 5а): `hub ui --print-url` печатает URL с кодом во фрагменте (не в query); `hub status`
работает; ни одна команда CLI (кроме `hub core`) не открывает файл БД — проверяется подменой
`sqlite3.connect` на падающую функцию. Спецификация: docs/architecture.md.

`hub core` поднимается в ОТДЕЛЬНОМ процессе (не в процессе теста): иначе подмена `sqlite3.connect` в
процессе теста сломала бы и сам сервер, который в реальной жизни всегда работает отдельно от CLI. CLI-команды
здесь вызываются как обычные функции Python (`hub.cli.main()`), а не через ещё один subprocess — это и есть
код, который проверяем.
"""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hub import cli as hub_cli

_STARTUP_TIMEOUT_S = 10.0


def _wait_for_file(path: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise TimeoutError(f"{path} не появился за {timeout} с")


def _wait_for_listening_socket(path: Path, timeout: float) -> None:
    """Файл `internal.sock` появляется на диске при `bind()`, до `listen()` — ждём, пока по нему реально
    можно ПОДКЛЮЧИТЬСЯ, иначе первый запрос теста мог бы прийти раньше, чем сервер начал принимать."""
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
    """Свободный порт вместо боевого 8787: тесты не должны ни мешать живому хабу, ни зависеть от него."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def core_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    data_dir.mkdir()
    config_dir.mkdir()

    env = {**os.environ, "HUB_DATA_DIR": str(data_dir), "HUB_CONFIG_DIR": str(config_dir)}
    proc = subprocess.Popen(  # noqa: S603 — фиксированный локальный интерпретатор, без пользовательского ввода
        [sys.executable, "-c", "from hub.cli import main; main()", "core", "--port", str(_free_port())],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_file(config_dir / "admin.key", _STARTUP_TIMEOUT_S)
        _wait_for_listening_socket(data_dir / "internal.sock", _STARTUP_TIMEOUT_S)
        monkeypatch.setenv("HUB_DATA_DIR", str(data_dir))
        monkeypatch.setenv("HUB_CONFIG_DIR", str(config_dir))
        yield data_dir, config_dir
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_ui_print_url_puts_code_in_fragment_not_query(core_env, capsys) -> None:
    _run_cli(["ui", "--print-url"])
    out = capsys.readouterr().out.strip()
    assert "#code=" in out
    assert "?code=" not in out
    assert out.startswith("http://127.0.0.1:8787/login")


def test_status_works(core_env, capsys) -> None:
    _run_cli(["status"])
    out = capsys.readouterr().out
    body = json.loads(out)
    assert "accounts" in body
    assert "max_seq" in body


def test_account_project_token_roundtrip(core_env, capsys) -> None:
    _run_cli(["project", "add", "--name", "Hub"])
    project_out = capsys.readouterr().out
    assert "project_id=" in project_out

    _run_cli(["account", "add", "--source", "tg", "--db", "main"])
    account_out = capsys.readouterr().out
    assert "account_id=" in account_out
    account_id = account_out.splitlines()[0].split("=")[1]
    key_path = Path(os.environ["HUB_CONFIG_DIR"]) / "connectors" / f"{account_id}.key"
    assert key_path.exists()
    assert oct(key_path.stat().st_mode & 0o777) == "0o600"

    _run_cli(["token", "create", "--name", "t1", "--kind", "interactive", "--scopes", "read"])
    token_out = capsys.readouterr().out.strip()
    assert token_out.startswith("hub_")


def test_no_cli_command_opens_the_database_file(core_env, monkeypatch, capsys) -> None:
    def _boom(*_args, **_kwargs):
        raise AssertionError("sqlite3.connect must not be called outside `hub core`")

    monkeypatch.setattr(sqlite3, "connect", _boom)

    _run_cli(["status"])
    capsys.readouterr()
    _run_cli(["ui", "--print-url"])
    capsys.readouterr()
    _run_cli(["project", "add", "--name", "P2"])
    capsys.readouterr()

    # Кусок 1b: `hub auth tg` завела бы настоящий аккаунт (через сокет, не sqlite3.connect — то, что этот
    # тест и проверяет) и тут же остановилась бы на интерактивном вводе api_id. `cmd_auth_tg` по умолчанию
    # зовёт `input_fn=input` — это ЗНАЧЕНИЕ ПО УМОЛЧАНИЮ связывается со встроенной `input` ОДИН РАЗ, при
    # определении функции (импорт `hub.connectors.telegram.auth` в других тестах пакета мог случиться раньше
    # этого теста), поэтому подменять здесь нужно не сам объект `input`, а источник, который он читает —
    # `sys.stdin`: пустой поток даёт `EOFError` при первом же вызове, до любого файла БД.
    monkeypatch.setattr(sys.stdin, "read", lambda *_a, **_kw: "")
    monkeypatch.setattr(sys.stdin, "readline", lambda *_a, **_kw: "")
    with pytest.raises((SystemExit, EOFError)):
        _run_cli(["auth", "tg"])
    capsys.readouterr()


# --- Тест 15 (кусок 1a фазы 1): hub map/hub folders/hub account resume — раздел B/C спеки
# docs/architecture.md. Работают через сокет, как всё остальное CLI, ни одна не открывает БД.


def _push_tg_folders(data_dir, config_dir, account_id: str, folders: list[dict]) -> None:
    """Завести `account_meta.tg_folders` для аккаунта секретом коннектора — как это сделал бы настоящий
    коннектор Telegram (событием `account_meta`, а не прямой записью в БД, которую CLI-тест не открывает)."""
    import httpx

    key_path = Path(config_dir) / "connectors" / f"{account_id}.key"
    secret = key_path.read_text(encoding="utf-8").strip()
    transport = httpx.HTTPTransport(uds=str(Path(data_dir) / "internal.sock"))
    with httpx.Client(
        transport=transport,
        base_url="http://hub-internal/internal/v1",
        headers={"Authorization": f"Bearer {secret}"},
    ) as client:
        r = client.post(
            "/events",
            json={"events": [{"type": "account_meta", "key": "tg_folders", "value": folders}]},
        )
        assert r.status_code == 200, r.text


def test_1a_15_map_add_unknown_folder_refuses_with_known_list(core_env, capsys) -> None:
    data_dir, config_dir = core_env
    # Ф14-1: папки проверяет ядро (`build_match`) — только у настоящего источника `telegram`.
    _run_cli(["account", "add", "--source", "telegram", "--db", "main"])
    account_out = capsys.readouterr().out
    account_id = account_out.splitlines()[0].split("=")[1]

    _run_cli(["project", "add", "--name", "Работа"])
    project_out = capsys.readouterr().out
    project_id = project_out.strip().split("=")[1]

    _push_tg_folders(data_dir, config_dir, account_id, [{"id": 3, "title": "Работа"}])

    with pytest.raises(SystemExit) as excinfo:
        _run_cli(
            ["map", "add", "--account", account_id, "--project", project_id, "--folder", "99"]
        )
    message = str(excinfo.value)
    assert "99" in message
    assert "неизвестна" in message
    assert "3=Работа" in message


def test_1a_15_map_list_add_rm_and_folders_and_account_resume(core_env, capsys) -> None:
    _run_cli(["account", "add", "--source", "tg", "--db", "main"])
    account_out = capsys.readouterr().out
    account_id = account_out.splitlines()[0].split("=")[1]

    _run_cli(["project", "add", "--name", "Работа"])
    project_out = capsys.readouterr().out
    project_id = project_out.strip().split("=")[1]

    # hub folders до первой сверки коннектора — вежливое сообщение, не падение.
    _run_cli(["folders", "--account", account_id])
    folders_out = capsys.readouterr().out
    assert "папок нет" in folders_out

    _run_cli(
        ["map", "add", "--account", account_id, "--project", project_id, "--channel", "c1", "--priority", "5"]
    )
    add_out = capsys.readouterr().out
    rule = json.loads(add_out)
    rule_id = rule["rule_id"]

    _run_cli(["map", "list", "--account", account_id])
    list_out = capsys.readouterr().out
    assert f'"id": {rule_id}' in list_out or f'"id":{rule_id}' in list_out

    _run_cli(["map", "rm", str(rule_id)])
    rm_out = capsys.readouterr().out
    assert json.loads(rm_out)["ok"] is True

    _run_cli(["account", "resume", account_id])
    resume_out = capsys.readouterr().out.strip()
    assert resume_out == "ok"


def test_account_set_parses_json_values_and_reports_restart(core_env, capsys) -> None:
    """`hub account set` (24.09): значения — JSON (`true`, список, `null` удаляет ключ), иначе строка; вывод —
    итоговый конфиг и напоминание перезапустить коннектор; несуществующий аккаунт — понятный отказ."""
    _run_cli(["account", "add", "--source", "mail", "--db", "main", "--config", "address=a@b.c"])
    account_id = capsys.readouterr().out.splitlines()[0].split("=")[1]

    _run_cli(["account", "set", account_id, "send_enabled=true", 'aliases=["x@b.c"]', "display_name=владелец"])
    out = capsys.readouterr().out
    config_text, _, tail = out.rpartition("}")
    assert json.loads(config_text + "}") == {
        "address": "a@b.c",
        "send_enabled": True,
        "aliases": ["x@b.c"],
        "display_name": "владелец",
    }
    assert f"systemctl --user restart hub-connector@{account_id}" in tail

    _run_cli(["account", "set", account_id, "aliases=null"])
    config_text, _, _ = capsys.readouterr().out.rpartition("}")
    assert "aliases" not in json.loads(config_text + "}")

    with pytest.raises(SystemExit, match="аккаунта 999999 нет"):
        _run_cli(["account", "set", "999999", "k=1"])
    with pytest.raises(SystemExit, match="ключ=значение"):
        _run_cli(["account", "set", account_id, "send_enabled"])


def test_account_rm_removes_empty_account_and_its_files(core_env, capsys) -> None:
    """`hub account rm` (04.10): пустой аккаунт удаляется вместе с секретом коннектора, кредами и каталогом
    неоконченной сессии Telegram; файлы соседнего аккаунта целы; несуществующий — понятный отказ."""
    data_dir, config_dir = core_env
    _run_cli(["account", "add", "--source", "telegram", "--db", "main"])
    keep = capsys.readouterr().out.splitlines()[0].split("=")[1]
    _run_cli(["account", "add", "--source", "telegram", "--db", "main"])
    account_id = capsys.readouterr().out.splitlines()[0].split("=")[1]
    connectors = config_dir / "connectors"
    (connectors / f"{account_id}.telegram.json").write_text("{}")
    session_dir = data_dir / "telegram" / account_id
    session_dir.mkdir(parents=True)
    (session_dir / "tg.session").write_text("")

    _run_cli(["account", "rm", account_id])
    out = capsys.readouterr().out
    assert f"аккаунт {account_id} (telegram) удалён" in out
    assert not (connectors / f"{account_id}.key").exists()
    assert not (connectors / f"{account_id}.telegram.json").exists()
    assert not session_dir.exists()
    assert (connectors / f"{keep}.key").exists()

    _run_cli(["status"])
    assert [a["id"] for a in json.loads(capsys.readouterr().out)["accounts"]] == [int(keep)]
    with pytest.raises(SystemExit, match=f"аккаунта {account_id} нет"):
        _run_cli(["account", "rm", account_id])


def test_map_add_from_domain_rule_lowercased_and_exclusive(core_env, capsys) -> None:
    """Почта 24.09: `hub map add --from-domain` -> правило `attr=from_domains has <домен>` (нижний регистр,
    без `@`); вместе с --label — отказ argparse; домен с `@` посередине — понятный отказ."""
    _run_cli(["account", "add", "--source", "mail", "--db", "main"])
    account_id = capsys.readouterr().out.splitlines()[0].split("=")[1]
    _run_cli(["project", "add", "--name", "Initech"])
    project_id = capsys.readouterr().out.strip().split("=")[1]

    base = ["map", "add", "--account", account_id, "--project", project_id]
    _run_cli([*base, "--from-domain", "@initech.Atlassian.net"])
    added = json.loads(capsys.readouterr().out)
    assert added["priority"] == 0
    _run_cli(["map", "list", "--account", account_id])
    listed = capsys.readouterr().out
    assert '"attr": "from_domains"' in listed
    assert '"has": "initech.atlassian.net"' in listed

    with pytest.raises(SystemExit):
        _run_cli([*base, "--from-domain", "a", "--label", "b"])
    with pytest.raises(SystemExit, match="домен вида"):
        _run_cli([*base, "--from-domain", "me@x.com"])


def test_f141_cli_refusals_print_core_detail(core_env, capsys) -> None:
    """Ф14-1 A5: отказы ядра 4xx (`hub project add` 409/422, `hub map add` 409/422) — текстом `detail` через
    SystemExit, без трейсбека HTTPStatusError."""
    _run_cli(["project", "add", "--name", "Личное"])
    project_id = capsys.readouterr().out.strip().split("=")[1]
    with pytest.raises(SystemExit, match="hub project add: проект «Личное» уже есть"):
        _run_cli(["project", "add", "--name", "личное"])
    with pytest.raises(SystemExit, match="hub project add: цвет проекта"):
        _run_cli(["project", "add", "--name", "Другое", "--color", "red"])

    _run_cli(["account", "add", "--source", "mail", "--db", "main"])
    account_id = capsys.readouterr().out.splitlines()[0].split("=")[1]
    base = ["map", "add", "--account", account_id, "--project", project_id]
    _run_cli([*base, "--label", "Счета"])
    rule_id = json.loads(capsys.readouterr().out)["rule_id"]
    with pytest.raises(SystemExit, match=rf"hub map add: такое правило уже есть \(#{rule_id}\)"):
        _run_cli([*base, "--label", "Счета"])
    with pytest.raises(SystemExit, match="hub map add: папки есть только у Telegram"):
        _run_cli([*base, "--folder", "2"])


def test_2b_10_map_add_all_and_mutually_exclusive_with_folder(core_env, capsys) -> None:
    """Раздел G спеки 2b: `hub map add --all` создаёт правило `{"all": true}` с приоритетом -100 по
    умолчанию (ядро решает); `--all` вместе с `--folder`/`--channel` — отказ argparse (взаимоисключающая
    группа), без обращения к сокету вовсе."""
    _run_cli(["account", "add", "--source", "slack", "--db", "work"])
    account_out = capsys.readouterr().out
    account_id = account_out.splitlines()[0].split("=")[1]

    _run_cli(["project", "add", "--name", "Globex"])
    project_out = capsys.readouterr().out
    project_id = project_out.strip().split("=")[1]

    _run_cli(["map", "add", "--account", account_id, "--project", project_id, "--all"])
    add_out = capsys.readouterr().out
    rule = json.loads(add_out)
    assert rule["priority"] == -100

    with pytest.raises(SystemExit):
        _run_cli(
            [
                "map",
                "add",
                "--account",
                account_id,
                "--project",
                project_id,
                "--all",
                "--folder",
                "3",
            ]
        )


def test_1a_15_new_cli_commands_do_not_open_the_database_file(core_env, monkeypatch, capsys) -> None:
    _run_cli(["account", "add", "--source", "tg", "--db", "main"])
    account_out = capsys.readouterr().out
    account_id = account_out.splitlines()[0].split("=")[1]
    _run_cli(["project", "add", "--name", "Работа"])
    project_out = capsys.readouterr().out
    project_id = project_out.strip().split("=")[1]

    def _boom(*_args, **_kwargs):
        raise AssertionError("sqlite3.connect must not be called outside `hub core`")

    monkeypatch.setattr(sqlite3, "connect", _boom)

    _run_cli(["folders", "--account", account_id])
    capsys.readouterr()
    _run_cli(["map", "list", "--account", account_id])
    capsys.readouterr()
    _run_cli(
        ["map", "add", "--account", account_id, "--project", project_id, "--channel", "c1"]
    )
    capsys.readouterr()
    _run_cli(["map", "list"])
    capsys.readouterr()
    _run_cli(["account", "resume", account_id])
    capsys.readouterr()


def _run_cli(argv: list[str]) -> None:
    old_argv = sys.argv
    sys.argv = ["hub", *argv]
    try:
        hub_cli.main()
    finally:
        sys.argv = old_argv


# SIGTERM гасит ОБА сервера одним обработчиком, процесс выходит сам с кодом 0 (а не убит перевыброшенным
# сигналом uvicorn до Db.close()), блокировка снята — следующий старт на тех же каталогах проходит.
def test_sigterm_stops_core_cleanly_and_releases_lock(tmp_path) -> None:
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    data_dir.mkdir()
    config_dir.mkdir()
    env = {**os.environ, "HUB_DATA_DIR": str(data_dir), "HUB_CONFIG_DIR": str(config_dir)}

    for _round in range(2):
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", "from hub.cli import main; main()", "core", "--port", str(_free_port())],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_for_listening_socket(data_dir / "internal.sock", _STARTUP_TIMEOUT_S)
            proc.terminate()
            assert proc.wait(timeout=15) == 0, proc.stdout.read().decode(errors="replace")[-2000:]
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


# --- Ф8-3 A6: hub project set --attention, hub project list, hub channel attention -------------


def test_f83_project_attention_list_and_channel_attention(core_env, capsys) -> None:
    import httpx

    from hub import paths

    _run_cli(["project", "add", "--name", "Globex"])
    _run_cli(["account", "add", "--source", "slack", "--db", "work"])
    account_id = capsys.readouterr().out.split("account_id=")[1].split()[0]
    key = (Path(os.environ["HUB_CONFIG_DIR"]) / "connectors" / f"{account_id}.key").read_text().strip()
    transport = httpx.HTTPTransport(uds=str(paths.data_dir() / "internal.sock"))
    with httpx.Client(transport=transport, base_url="http://hub-internal/internal/v1") as client:
        event = {
            "type": "upsert",
            "channel": {"ext_id": "C1", "kind": "group", "title": "#general"},
            "ext_id": "1.0",
            "ts": 1000,
            "is_me": False,
            "kind": "text",
            "text": "x",
            "mentions_me": False,
        }
        r = client.post("/events", json={"events": [event]}, headers={"Authorization": f"Bearer {key}"})
        assert r.json()["applied"] == 1
    channel_id = str(2**40 + 1)  # первый канал файла work
    _run_cli(["channel", "show", channel_id])
    assert json.loads(capsys.readouterr().out)["attention"] == "info"  # Неразобранное (Ф57-1)
    _run_cli(["channel", "move", channel_id, "--project", "1"])
    capsys.readouterr()

    _run_cli(["project", "set", "1", "--attention", "group=react", "mail_thread=info"])
    out = json.loads(capsys.readouterr().out)
    assert out["attention_policy"] == {"group": "react", "mail_thread": "info"}
    assert "workdir" not in out
    _run_cli(["project", "set", "1", "--attention", "group=null"])
    assert json.loads(capsys.readouterr().out)["attention_policy"] == {"mail_thread": "info"}
    _run_cli(["project", "list"])
    listed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert listed == [
        {
            "id": 1,
            "name": "Globex",
            "muted": False,
            "workdir": None,
            "attention": {"mail_thread": "info"},
            "subs": False,  # Ф13-1 B0
            # Ф15-1 E: реакции сессий — выключены; паузы по умолчанию нет (держит сессия, владелец 29.09).
            "session_reactions": {"sessions": False, "delay_min_s": 0, "delay_max_s": 0},
            "join": "confirm",  # Ф17-1 E: вступление сессий — по умолчанию через владельца
            "session_handled": False,  # Ф37-3 A: отметка «разобрано» — по умолчанию выключена
        }
    ]

    # Ф15-1 E: реакции сессий и их пауза.
    _run_cli(["project", "set", "1", "--session-reactions", "on", "--reaction-delay", "1-3"])
    out = json.loads(capsys.readouterr().out)
    assert out["session_reactions"] == {"sessions": True, "delay_min_s": 60, "delay_max_s": 180}
    _run_cli(["project", "list"])
    assert json.loads(capsys.readouterr().out)["session_reactions"]["sessions"] is True
    with pytest.raises(SystemExit, match="reaction-delay"):
        _run_cli(["project", "set", "1", "--reaction-delay", "5-2"])
    with pytest.raises(SystemExit, match="reaction-delay"):
        _run_cli(["project", "set", "1", "--reaction-delay", "0-61"])

    # Ф17-1 E: вступление сессий в группы и каналы Telegram.
    _run_cli(["project", "set", "1", "--session-join", "auto"])
    assert json.loads(capsys.readouterr().out)["session_join"] == "auto"
    _run_cli(["project", "list"])
    assert json.loads(capsys.readouterr().out)["join"] == "auto"
    _run_cli(["project", "set", "1", "--session-join", "confirm"])
    capsys.readouterr()
    _run_cli(["project", "list"])
    assert json.loads(capsys.readouterr().out)["join"] == "confirm"

    # Ф37-3 A: отметка «разобрано» сессиями проекта — по умолчанию выключена.
    _run_cli(["project", "set", "1", "--session-handled", "on"])
    assert json.loads(capsys.readouterr().out)["session_handled"] is True
    _run_cli(["project", "list"])
    assert json.loads(capsys.readouterr().out)["session_handled"] is True
    _run_cli(["project", "set", "1", "--session-handled", "off"])
    assert json.loads(capsys.readouterr().out)["session_handled"] is False

    _run_cli(["channel", "attention", channel_id, "react"])
    assert json.loads(capsys.readouterr().out) == {
        "id": int(channel_id),
        "attention": "react",
        "attention_manual": "react",
        "attention_auto": "info",
    }
    _run_cli(["channel", "attention", channel_id, "auto"])
    assert json.loads(capsys.readouterr().out)["attention_manual"] is None

    with pytest.raises(SystemExit, match="неизвестный режим"):
        _run_cli(["project", "set", "1", "--attention", "dm=loud"])
    with pytest.raises(SystemExit, match="неизвестный режим"):
        _run_cli(["project", "set", "1", "--attention", "dm=digest"])  # Ф57-2: режима «сводка» нет
    with pytest.raises(SystemExit, match="укажите"):
        _run_cli(["project", "set", "1"])
    with pytest.raises(SystemExit, match="проекта 7 нет"):
        _run_cli(["project", "set", "7", "--attention", "dm=react"])
    with pytest.raises(SystemExit):
        _run_cli(["channel", "attention", "99", "react"])


# --- Ф11-1 C (тест 23): hub channel wait — JSON ответа, --follow несёт курсор, 404 -> код 1 ----------------


def _wait_body(*, after, cursor, woke, text=None):
    messages = [{"id": cursor, "text": text, "wait_class": "new"}] if text else []
    return {
        "channel_id": 145,
        "after": after,
        "cursor": cursor,
        "woke": woke,
        "more": False,
        "from_now": after is None,
        "waited_ms": 5,
        "messages": messages,
    }


def _fake_admin(monkeypatch, responses):
    """Подменный транспорт админского сокета: отдаёт `responses` по очереди, пишет запросы в список."""
    import httpx

    seen: list[httpx.Request] = []
    seen_timeouts: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        status, body = item
        return httpx.Response(status, json=body)

    def _client(*, timeout=None):
        seen_timeouts.append(timeout)
        return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://hub-internal/internal/v1")

    monkeypatch.setattr(hub_cli, "_admin_client", _client)
    return seen, seen_timeouts


def test_f11_23_channel_wait_once_prints_json(monkeypatch, capsys) -> None:
    body = _wait_body(after=7, cursor=7, woke=False)
    seen, timeouts = _fake_admin(monkeypatch, [(200, body)])
    _run_cli(["channel", "wait", "145", "--after", "7", "--timeout", "30", "--thread", "171.1"])
    assert json.loads(capsys.readouterr().out) == body
    [request] = seen
    assert request.url.path == "/internal/v1/admin/channels/145/wait"
    assert dict(request.url.params) == {"after": "7", "timeout_s": "30", "thread": "171.1", "skip_mine": "1"}
    assert timeouts == [45]

    seen, _ = _fake_admin(monkeypatch, [(404, {"detail": "not found"})])
    with pytest.raises(SystemExit) as exc:
        _run_cli(["channel", "wait", "999", "--include-mine"])
    assert exc.value.code != 0 and "канала 999 нет" in str(exc.value.code)
    assert dict(seen[0].url.params) == {"timeout_s": "60", "skip_mine": "0"}


def test_f11_23_channel_wait_follow_carries_cursor(monkeypatch, capsys) -> None:
    responses = [
        (200, _wait_body(after=None, cursor=5, woke=True, text="первое")),
        (200, _wait_body(after=5, cursor=5, woke=False)),  # таймаут — молча
        (200, _wait_body(after=5, cursor=6, woke=False)),  # правка без будящих — молча, курсор дальше
        (200, _wait_body(after=6, cursor=9, woke=True, text="второе")),
        (404, {"detail": "not found"}),
    ]
    seen, _ = _fake_admin(monkeypatch, responses)
    with pytest.raises(SystemExit) as exc:
        _run_cli(["channel", "wait", "145", "--follow", "--timeout", "10"])
    assert exc.value.code not in (0, None, 130)
    out = capsys.readouterr().out.splitlines()
    assert [json.loads(line)["cursor"] for line in out] == [5, 9]
    assert all(": " not in line for line in out)  # компактный JSON, по строке на пробуждение
    assert [r.url.params.get("after") for r in seen] == [None, "5", "5", "6", "9"]

    _fake_admin(monkeypatch, [(200, _wait_body(after=None, cursor=5, woke=False)), KeyboardInterrupt()])
    with pytest.raises(SystemExit) as exc:
        _run_cli(["channel", "wait", "145", "--follow"])
    assert exc.value.code == 130


# --- Ф11-9c: hub channel archive ID … [--undo] — строка на беседу, беседы нет -> сказать и продолжить -------


def _archive_body(channel_id, *, archived, closed):
    title = f"[Jira] (A-{channel_id})"
    return {"id": channel_id, "archived": archived, "closed_cards": closed, "title": title}


def test_f119c_channel_archive_many_missing_continues(monkeypatch, capsys) -> None:
    responses = [
        (200, _archive_body(11, archived=True, closed=2)),
        (404, {"detail": "канал 12 не найден"}),
        (200, _archive_body(13, archived=True, closed=0)),
    ]
    seen, _ = _fake_admin(monkeypatch, responses)
    _run_cli(["channel", "archive", "11", "12", "13"])
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert out == [
        {"id": 11, "archived": True, "closed_cards": 2, "title": "[Jira] (A-11)"},
        {"id": 12, "error": "беседы нет"},
        {"id": 13, "archived": True, "closed_cards": 0, "title": "[Jira] (A-13)"},
    ]
    assert [(r.method, r.url.path) for r in seen] == [
        ("PATCH", "/internal/v1/admin/channels/11"),
        ("PATCH", "/internal/v1/admin/channels/12"),
        ("PATCH", "/internal/v1/admin/channels/13"),
    ]
    assert all(json.loads(r.content) == {"archived": True} for r in seen)


def test_f119c_channel_archive_undo(monkeypatch, capsys) -> None:
    seen, _ = _fake_admin(monkeypatch, [(200, _archive_body(11, archived=False, closed=0))])
    _run_cli(["channel", "archive", "--undo", "11"])
    assert json.loads(capsys.readouterr().out)["archived"] is False
    assert json.loads(seen[0].content) == {"archived": False}


# --- Ф37-1 A3: hub channel autosend ID --role РОЛЬ on|off / list -----------------------------------------


def test_f37_channel_autosend_on_off_and_list(monkeypatch, capsys) -> None:
    answer = {"id": 283, "autosend": ["маркетолог"], "changed": True}
    seen, _ = _fake_admin(monkeypatch, [(200, answer)])
    _run_cli(["channel", "autosend", "283", "--role", "маркетолог", "on"])
    assert json.loads(capsys.readouterr().out) == answer
    [request] = seen
    assert (request.method, request.url.path) == ("PUT", "/internal/v1/admin/channels/283/autosend")
    assert json.loads(request.content) == {"role": "маркетолог", "on": True}

    seen, _ = _fake_admin(monkeypatch, [(200, {"id": 283, "autosend": [], "changed": True})])
    _run_cli(["channel", "autosend", "283", "--role", "маркетолог", "off"])
    assert json.loads(seen[0].content) == {"role": "маркетолог", "on": False}
    capsys.readouterr()

    items = [{"channel_id": 283, "role": "маркетолог", "created_ts": 1, "title": "Команда", "project_id": 3}]
    seen, _ = _fake_admin(monkeypatch, [(200, {"items": items})])
    _run_cli(["channel", "autosend", "list"])
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == items
    assert (seen[0].method, seen[0].url.path) == ("GET", "/internal/v1/admin/autosend")


def test_f37_channel_autosend_refusals(monkeypatch) -> None:
    seen, _ = _fake_admin(monkeypatch, [(422, {"detail": "беседа в архиве"})])
    with pytest.raises(SystemExit) as exc:
        _run_cli(["channel", "autosend", "283", "--role", "маркетолог", "on"])
    assert "беседа в архиве" in str(exc.value.code)

    for argv in (
        ["283", "on"],
        ["283", "--role", "маркетолог"],
        ["list", "on"],
        ["abc", "--role", "x", "on"],
    ):
        seen, _ = _fake_admin(monkeypatch, [])
        with pytest.raises(SystemExit) as exc:
            _run_cli(["channel", "autosend", *argv])
        assert exc.value.code not in (0, None) and seen == []


def _ph83_engine_body(**extra) -> dict:
    body = {
        "engine": "claude",
        "model": "sonnet",
        "effort": None,
        "changed_ts": None,
        "engines": [],
        "facts": {"engine": "claude", "model": "haiku", "effort": None, "follows_agent": True},
        "agent_fallback": None,
        "facts_fallback": None,
        "fallback": None,
        "on_fallback": None,
    }
    body.update(extra)
    return body


def test_ph83_engine_fallback_set_off_return_and_show(monkeypatch, capsys) -> None:
    """Ф83-1: `hub engine fallback <agent|facts> [set|off|return]` — админ-сокет, строки запасного."""
    on = {"main": {"engine": "claude"}, "kind": "limit", "reason": "до 18:00", "since": 1_760_000_000_000}
    shown = _ph83_engine_body(
        agent_fallback={"engine": "codex", "model": None, "effort": "low", "ts": 1},
        fallback={"engine": "codex", "model": "gpt-6.1-sol", "effort": "low"},
        on_fallback=on,
    )
    seen, _ = _fake_admin(monkeypatch, [(200, shown)])
    _run_cli(["engine", "fallback", "agent", "set", "codex", "--effort", "low"])
    out = capsys.readouterr().out
    assert seen[0].url.path == "/internal/v1/admin/engine/fallback/agent"
    assert json.loads(seen[0].content) == {"engine": "codex", "effort": "low"}
    assert "запасной (агент): codex · gpt-6.1-sol · low — НА ЗАПАСНОМ" in out
    assert "hub engine fallback agent return" in out and "запасной (факты): нет" in out

    seen, _ = _fake_admin(monkeypatch, [(200, _ph83_engine_body())])
    _run_cli(["engine", "fallback", "facts", "off"])
    assert json.loads(seen[0].content) == {"engine": None}
    assert seen[0].url.path == "/internal/v1/admin/engine/fallback/facts"
    capsys.readouterr()

    seen, _ = _fake_admin(monkeypatch, [(200, {**_ph83_engine_body(), "returned": False})])
    _run_cli(["engine", "fallback", "agent", "return"])
    assert seen[0].url.path == "/internal/v1/admin/engine/fallback/agent/return"
    assert "не был на запасном" in capsys.readouterr().out

    _fake_admin(monkeypatch, [(422, {"detail": "engine: запасной — другой движок, не основной"})])
    with pytest.raises(SystemExit) as exc:
        _run_cli(["engine", "fallback", "agent", "set", "claude"])
    assert "HTTP 422" in str(exc.value.code) and "другой движок" in str(exc.value.code)

    # `hub engine` показывает строки запасных под выбором фактов; ядро без них — без строк.
    _fake_admin(monkeypatch, [(200, _ph83_engine_body()), (200, {"engine": "claude", "model": "sonnet"})])
    _run_cli(["engine"])
    assert "запасной (агент): нет" in capsys.readouterr().out
    _run_cli(["engine"])
    assert "запасной" not in capsys.readouterr().out


# --- Ф86-1: выключатели сводки дня, ключ клиента сводки, `hub digest show` ----------------------------------


def test_f86_project_digest_switches_and_digest_key(core_env, capsys) -> None:
    _run_cli(["project", "add", "--name", "Hub"])
    capsys.readouterr()
    _run_cli(["project", "set", "1", "--digest", "off", "--siblings", "off"])
    out = json.loads(capsys.readouterr().out)
    assert (out["digest_after_compact"], out["sibling_says"]) == (False, False)
    _run_cli(["project", "list"])
    listed = json.loads(capsys.readouterr().out)
    assert (listed["digest"], listed["siblings"]) == ("off", "off")
    _run_cli(["project", "set", "1", "--digest", "on", "--siblings", "on"])
    capsys.readouterr()
    _run_cli(["project", "list"])
    listed = json.loads(capsys.readouterr().out)
    assert "digest" not in listed and "siblings" not in listed  # включены — строка прежняя

    _run_cli(["digest", "token", "--print"])
    captured = capsys.readouterr()
    key = captured.out.strip()
    assert key.startswith("hub_") and "ключ сводки дня выпущен" in captured.err
    key_file = Path(os.environ["HUB_CONFIG_DIR"]) / "digest.key"
    assert json.loads(key_file.read_text())["key"] == key and oct(key_file.stat().st_mode & 0o777) == "0o600"
    _run_cli(["digest", "token"])
    captured = capsys.readouterr()
    assert captured.out == "" and "прежний ключ" in captured.err and "отозван" in captured.err
    _run_cli(["digest", "revoke"])
    assert "отозван" in capsys.readouterr().err and not key_file.exists()

    # Сводки нет — пустой вывод, код 0 (хук ручных окон не шумит).
    _run_cli(["digest", "show", "--project", "1"])
    assert capsys.readouterr().out == ""


def test_f86_digest_show_prints_block_or_nothing(monkeypatch, capsys, tmp_path) -> None:
    import httpx

    block = '<hub_digest at="10:00" project="P">\nсводка\n</hub_digest>'
    seen, timeouts = _fake_admin(monkeypatch, [(200, {"project_id": 1, "block": block})])
    _run_cli(["digest", "show", "--cwd", str(tmp_path / "src"), "--max", "6000"])
    assert capsys.readouterr().out == block + "\n"
    assert dict(seen[0].url.params) == {"max": "6000", "cwd": str(tmp_path / "src")}
    assert seen[0].url.path == "/internal/v1/admin/digest" and timeouts == [10.0]
    answers = ((200, {"project_id": None, "block": None}), (422, {"detail": "x"}), httpx.ConnectError("нет"))
    for answer in answers:
        _fake_admin(monkeypatch, [answer])
        _run_cli(["digest", "show", "--project", "3", "--day", "2026-10-22"])
        assert capsys.readouterr().out == ""
