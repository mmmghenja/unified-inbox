"""`hub auth mail` (спека 5a, тест 1), пароль нигде (тест 14), CLI: `hub connector` для почты, `hub map add
--label`. Настоящий `hub core` в отдельном процессе (как у Slack), Gmail — `FakeGmail`."""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from hub.connectors.mail.auth import cmd_auth_mail
from hub.connectors.mail.gateway import GmailGateway

from .conftest import make_connector, sync_admin_client
from .fakegmail import FakeGmail
from .helpers import incoming, init_cursor

ADDRESS = "me@gmail.com"


def _args(**kw) -> SimpleNamespace:
    base = {
        "address": ADDRESS,
        "title": None,
        "db": None,
        "account": None,
        "proxy": None,
        "receive_only": False,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _factory(gm: FakeGmail):
    def _make(*, address, password, proxy):
        return GmailGateway(
            address=address,
            password=password,
            proxy=proxy,
            imap_factory=gm.imap_factory,
            smtp_factory=gm.smtp_factory,
        )

    return _make


def _run(core_env, gm: FakeGmail, *, password: str | None = None, **kw) -> tuple[int, list[str]]:
    data_dir, config_dir = core_env
    printed: list[str] = []
    with sync_admin_client(config_dir, data_dir) as admin:
        code = cmd_auth_mail(
            _args(**kw),
            admin_client=admin,
            config_dir=config_dir,
            gateway_factory=_factory(gm),
            getpass_fn=lambda _p: password if password is not None else "abcd efgh ijkl mnop",
            print_fn=printed.append,
        )
    return code, printed


def _accounts(core_env) -> list[dict]:
    data_dir, config_dir = core_env
    with sync_admin_client(config_dir, data_dir) as admin:
        return [a for a in admin.get("/admin/status").json()["accounts"] if a["source"] == "mail"]


def _files(config_dir) -> list[str]:
    d = config_dir / "connectors"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def test_1_success_creates_account_and_0600_secret(core_env):
    data_dir, config_dir = core_env
    gm = FakeGmail()
    code, printed = _run(core_env, gm, db="work", title="Globex")
    assert code == 0, printed
    [acc] = _accounts(core_env)
    assert acc["title"] == "Globex"
    creds = config_dir / "connectors" / f"{acc['id']}.mail.json"
    assert stat.S_IMODE(creds.stat().st_mode) == 0o600
    assert json.loads(creds.read_text()) == {"password": gm.password}
    key = config_dir / "connectors" / f"{acc['id']}.key"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    with sync_admin_client(config_dir, data_dir) as admin:
        me = admin.get("/account", headers={"Authorization": f"Bearer {key.read_text().strip()}"}).json()
    config = me["config"]
    assert config["address"] == ADDRESS and config["provider"] == "gmail"
    assert config["display_name"] == "Globex"
    assert "CONDSTORE" in config["capabilities"]
    assert gm.password not in json.dumps(me)
    # проверка шла по IMAP и SMTP (EHLO/AUTH без отправки)
    steps = [e[0] for e in gm.smtp_log]
    assert "login" in steps and "data" not in steps and "mail" not in steps
    assert any(c.startswith("LIST") for c in gm.command_texts())


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("bad_password", "не принял"),
        ("imap_disabled", "выключен IMAP"),
        ("no_gm_ext", "не годится"),
        ("no_all", "не годится"),
        ("no_sent", "не годится"),
        ("smtp_auth", "не принял"),
        ("no_network", "недоступен"),
    ],
)
def test_1_failures_write_nothing(core_env, capsys, setup, reason):
    _data_dir, config_dir = core_env
    gm = FakeGmail()
    password = None
    if setup == "bad_password":
        password = "wrongwrongwrong1"
    elif setup == "imap_disabled":
        gm.login_error = b"[ALERT] Your account is not enabled for IMAP use. Please visit your Gmail settings"
    elif setup == "no_gm_ext":
        gm.capabilities.remove("X-GM-EXT-1")
    elif setup == "no_all":
        gm.list_lines = [line for line in gm.list_lines if b"\\All" not in line]
    elif setup == "no_sent":
        gm.list_lines = [line for line in gm.list_lines if b"\\Sent" not in line]
    elif setup == "smtp_auth":
        import aiosmtplib

        gm.smtp_faults["login"] = aiosmtplib.SMTPAuthenticationError(535, "5.7.8 not accepted")
    elif setup == "no_network":
        gm.connect_errors = [OSError("network unreachable")]
    code, _printed = _run(core_env, gm, password=password)
    assert code == 1
    err = capsys.readouterr().err
    assert reason in err and "Ничего не записано" in err
    assert _accounts(core_env) == []
    assert _files(config_dir) == []
    if password:
        assert password not in err


def test_1_relogin_with_account_does_not_create_second(core_env):
    _data_dir, config_dir = core_env
    gm = FakeGmail()
    assert _run(core_env, gm)[0] == 0
    [acc] = _accounts(core_env)
    gm.password = "newpasswordnew12"
    code, printed = _run(core_env, gm, password="newpasswordnew12", account=acc["id"])
    assert code == 0, printed
    assert [a["id"] for a in _accounts(core_env)] == [acc["id"]]
    creds = config_dir / "connectors" / f"{acc['id']}.mail.json"
    assert json.loads(creds.read_text()) == {"password": "newpasswordnew12"}


def test_1_relogin_other_address_refused(core_env, capsys):
    gm = FakeGmail()
    assert _run(core_env, gm)[0] == 0
    [acc] = _accounts(core_env)
    code, _p = _run(
        core_env, FakeGmail(address="other@gmail.com"), address="other@gmail.com", account=acc["id"]
    )
    assert code == 1
    assert "другой адрес" in capsys.readouterr().err


def test_1_second_run_without_account_refused(core_env, capsys):
    gm = FakeGmail()
    assert _run(core_env, gm)[0] == 0
    code, _p = _run(core_env, gm)
    assert code == 1
    assert "--account" in capsys.readouterr().err
    assert len(_accounts(core_env)) == 1


def test_14_password_not_logged_during_auth(core_env, caplog):
    gm = FakeGmail()
    caplog.set_level(logging.DEBUG)
    code, printed = _run(core_env, gm)
    assert code == 0
    assert gm.password not in caplog.text
    assert all(gm.password not in p for p in printed)


async def test_14_password_not_in_connector_logs(admin_client, internal_transport, caplog, db):
    import aiosmtplib

    from hub.core import outbox as outbox_mod

    gm = FakeGmail()
    caplog.set_level(logging.DEBUG)
    connector, gw, _client, _aid = await make_connector(admin_client, internal_transport, gm)
    try:
        await init_cursor(connector)
        parent = incoming(gm)
        await connector.sync("live")
        token_id = await db.write(
            lambda c: (
                c.execute("INSERT INTO main.api_token (name, kind, hash) VALUES ('u','ui','x')").lastrowid
            )
        )

        def _enq(conn):
            cid = conn.execute(
                "SELECT id FROM main.channel WHERE ext_id = ?", (str(parent.thrid),)
            ).fetchone()[0]
            return outbox_mod.enqueue(
                conn,
                channel_id=cid,
                text="ok",
                origin="ui",
                api_token_id=token_id,
                idempotency_key=None,
                now_ms=1_700_000_000_000,
            )

        await db.write(_enq)
        gm.smtp_faults["data"] = aiosmtplib.SMTPServerDisconnected("lost")
        await connector.step_outbox()
        gm.login_error = b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)"
        await gw.reset_sessions()
        with pytest.raises(Exception) as ei:  # noqa: PT011
            await connector.sync("live")
        assert gm.password not in str(ei.value)
    finally:
        await gw.close()
    assert gm.password not in caplog.text
    assert "Пётр" not in caplog.text and "Привет" not in caplog.text  # тексты и имена — не в журнал


# --- CLI ----------------------------------------------------------------------------------------------------


def _hub(core_env, *argv: str) -> subprocess.CompletedProcess:
    data_dir, config_dir = core_env
    env = {**os.environ, "HUB_DATA_DIR": str(data_dir), "HUB_CONFIG_DIR": str(config_dir)}
    return subprocess.run(  # noqa: S603 — фиксированный локальный интерпретатор
        [sys.executable, "-c", "from hub.cli import main; main()", *argv],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_cli_connector_without_creds_exits_78(core_env):
    data_dir, config_dir = core_env
    with sync_admin_client(config_dir, data_dir) as admin:
        body = admin.post(
            "/admin/accounts",
            json={"source": "mail", "title": ADDRESS, "db": "main", "config": {"address": ADDRESS}},
        ).json()
    connectors = config_dir / "connectors"
    connectors.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = connectors / f"{body['account_id']}.key"
    key.write_text(body["connector_secret"])
    key.chmod(0o600)
    proc = _hub(core_env, "connector", str(body["account_id"]))
    assert proc.returncode == 78, proc.stderr
    assert "hub auth mail" in proc.stderr


def test_cli_map_add_label(core_env):
    data_dir, config_dir = core_env
    with sync_admin_client(config_dir, data_dir) as admin:
        acc = admin.post(
            "/admin/accounts",
            json={"source": "mail", "title": ADDRESS, "db": "main", "config": {"address": ADDRESS}},
        ).json()["account_id"]
        project = admin.post("/admin/projects", json={"name": "Initech"}).json()["project_id"]
    proc = _hub(core_env, "map", "add", "--account", str(acc), "--project", str(project), "--label", "Работа")
    assert proc.returncode == 0, proc.stderr
    with sync_admin_client(config_dir, data_dir) as admin:
        rules = admin.get("/admin/map-rules", params={"account_id": acc}).json()
    assert rules[0]["match"] == {"attr": "labels", "has": "Работа", "label": "метка «Работа»"}
    assert rules[0]["priority"] == 0


def test_cli_auth_mail_parser_accepts_flags():
    from hub.cli import _build_parser

    args = _build_parser().parse_args(
        ["auth", "mail", "--address", "a@b.c", "--db", "work", "--account", "3", "--proxy", "socks5://h:1"]
    )
    assert (args.source, args.address, args.db, args.account, args.proxy) == (
        "mail",
        "a@b.c",
        "work",
        3,
        "socks5://h:1",
    )


# --- живой запуск 24.09: SMTP Gmail из сети владельца закрыт — режим «только приём»


def _smtp_blocked(gm: FakeGmail) -> None:
    import aiosmtplib

    gm.smtp_faults["connect"] = aiosmtplib.SMTPConnectTimeoutError("Timed out connecting to smtp.gmail.com")


def test_smtp_blocked_suggests_receive_only(core_env, capsys):
    _data_dir, config_dir = core_env
    gm = FakeGmail()
    _smtp_blocked(gm)
    code, _printed = _run(core_env, gm)
    assert code == 1
    err = capsys.readouterr().err
    assert "--receive-only" in err and "Ничего не записано" in err
    assert _accounts(core_env) == []
    assert _files(config_dir) == []


def test_receive_only_skips_smtp_and_disables_send(core_env):
    gm = FakeGmail()
    _smtp_blocked(gm)
    code, printed = _run(core_env, gm, receive_only=True)
    assert code == 0, printed
    assert any("только приём" in line for line in printed)
    assert not any(step[0] == "connect" for step in gm.smtp_log)
    (acc,) = _accounts(core_env)
    assert acc["source"] == "mail"
