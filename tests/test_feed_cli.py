"""Ф53-1 (`docs/architecture.md`): `hub feed push` — лента от внешней программы.

Ядро — настоящее внутреннее приложение поверх ASGI (как `tests/internal/`), клиент — `CoreClient` из
`feed.run_push(transport=…)`; сокета нет. Тесты синхронные: `run_push` сам крутит цикл событий."""

import argparse
import io
import json

import httpx
import pytest

from hub import feed, paths
from hub.core import attention, connector_auth, ingest, queries
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.feed import Notifier

NOW_S = 1_700_000_000.0
ADMIN = ADMIN_PREFIX + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHI"


@pytest.fixture
def env(db, tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HUB_CONFIG_DIR", str(tmp_path / "cfg"))
    notifier = Notifier()
    db.on_commit(notifier.notify)
    app = create_internal_app(db, clock=lambda: NOW_S, notifier=notifier, admin_key_hash=hash_secret(ADMIN))
    return db, httpx.ASGITransport(app=app)


def _account(db, *, source="feed", title="Hooli.ru", key=True, secret=None) -> int:
    account_id = db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, title) VALUES (?, 'main', ?)", (source, title)
            ).lastrowid
        )
    )
    secret = secret or connector_auth.generate_connector_secret()
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.connector_secret (account_id, hash, created_ts) VALUES (?, ?, 0)",
            (account_id, connector_auth.hash_secret(secret)),
        )
    )
    if key:
        _write_key(account_id, secret)
    return account_id


def _write_key(account_id: int, secret: str) -> None:
    path = paths.config_dir() / "connectors" / f"{account_id}.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret, encoding="utf-8")


def _args(account, ext="101", *, text="Новое задание", **kw) -> argparse.Namespace:
    base = {
        "account": account,
        "ext": ext,
        "feed": "main",
        "feed_title": None,
        "sender": None,
        "ts": 5_000,
        "note": False,
        "alert": False,
        "text": text,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def _push(env, args, **kw) -> int:
    return feed.run_push(args, transport=env[1], **kw)


def _channels(db):
    with db.reader() as conn:
        return conn.execute(
            "SELECT id, ext_id, kind, title, meta_json FROM main.channel ORDER BY id"
        ).fetchall()


def _messages(db):
    with db.reader() as conn:
        return conn.execute(
            "SELECT m.ext_id, m.kind, m.text, m.is_me, m.is_bot FROM main.message m ORDER BY m.id"
        ).fetchall()


def test_push_creates_task_channel_no_reply(env, capsys):
    db = env[0]
    account = _account(db)
    assert _push(env, _args(account)) == 0
    assert capsys.readouterr().out.strip() == "ok"
    (chan,) = _channels(db)
    assert (chan[1], chan[2], chan[3]) == ("feed:main", "task", "Hooli.ru")
    meta = json.loads(chan[4])
    assert meta["attrs"]["no_reply"] is True and meta["attrs"]["feed"] == "main"
    (msg,) = _messages(db)
    assert (msg[0], msg[1], msg[2], msg[3]) == ("101", "text", "Новое задание", 0)
    assert msg[4] == 0  # не бот: подписка на проект бота отсеивает
    assert queries._reply_fields(chan[4])["can_reply"] is False


def test_repeat_is_noop(env, capsys):
    db = env[0]
    account = _account(db)
    assert _push(env, _args(account)) == 0
    capsys.readouterr()
    assert _push(env, _args(account)) == 0
    assert capsys.readouterr().out.strip() == "noop"
    assert len(_messages(db)) == 1


def test_second_feed_is_second_channel(env):
    db = env[0]
    account = _account(db)
    assert _push(env, _args(account)) == 0
    assert _push(env, _args(account, "7", feed="fl", feed_title="FL")) == 0
    assert [(c[1], c[3]) for c in _channels(db)] == [("feed:main", "Hooli.ru"), ("feed:fl", "FL")]


def test_note_is_event_and_not_unread(env):
    db = env[0]
    account = _account(db)
    assert _push(env, _args(account, "1", note=True, text="жив, заходов 3")) == 0
    (msg,) = _messages(db)
    assert msg[1] == "event"
    with db.reader() as conn:
        (cid,) = conn.execute("SELECT id FROM main.channel").fetchone()
        assert ingest.unread_count(conn, cid) == 0
    assert _push(env, _args(account, "2")) == 0
    with db.reader() as conn:
        assert ingest.unread_count(conn, cid) == 1  # обычное сообщение копит, служебная строка — нет


def test_non_feed_account_refused(env, capsys):
    db = env[0]
    account = _account(db, source="fake")
    assert _push(env, _args(account)) == 2
    assert "не является лентой" in capsys.readouterr().err
    assert _messages(db) == [] and _channels(db) == []


def test_no_key_file_is_78(env, capsys):
    account = _account(env[0], key=False)
    assert _push(env, _args(account)) == 78


def test_wrong_secret_is_78(env):
    account = _account(env[0])
    _write_key(account, connector_auth.generate_connector_secret())
    assert _push(env, _args(account)) == 78


def test_core_unavailable_is_1(env, capsys):
    account = _account(env[0])
    # без transport -> настоящий сокет `<data_dir>/internal.sock`, которого нет
    assert feed.run_push(_args(account)) == 1
    assert "недоступно" in capsys.readouterr().err


def test_empty_text_is_2_and_stdin(env, capsys):
    db = env[0]
    account = _account(db)
    assert _push(env, _args(account, text="   ")) == 2
    assert _push(env, _args(account, text=None), stdin=io.StringIO("")) == 2
    assert _messages(db) == []
    assert _push(env, _args(account, text=None), stdin=io.StringIO("из stdin")) == 0
    assert _messages(db)[0][2] == "из stdin"


def test_bad_ext_and_feed_key_is_2(env):
    account = _account(env[0])
    assert _push(env, _args(account, "")) == 2
    assert _push(env, _args(account, "x" * 201)) == 2
    assert _push(env, _args(account, feed="Bad Key")) == 2


def test_long_text_clipped(env):
    db = env[0]
    account = _account(db)
    assert _push(env, _args(account, text="я" * 9000)) == 0
    text = _messages(db)[0][2]
    assert len(text) == 8000 and text.endswith("…")


def test_message_signals_chan_sub_note_does_not(env):
    db = env[0]
    account = _account(db)
    assert _push(env, _args(account, "0", note=True, text="старт")) == 0
    with db.reader() as conn:
        (cid,) = conn.execute("SELECT id FROM main.channel").fetchone()
    token = db.write_sync(
        lambda conn: (
            conn.execute("INSERT INTO main.api_token (name, kind, hash) VALUES ('t', 'ui', 'x')").lastrowid
        )
    )
    now_ms = int(NOW_S * 1000)
    sub = db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.session_chan_sub (channel_id, project_id, api_token_id, session_pid, "
                "session_proc_start, session_id, session_cwd, created_ts) "
                "VALUES (?, NULL, ?, 1, 1, 's', '/w', ?)",
                (cid, token, now_ms - 1000),
            ).lastrowid
        )
    )

    def signals():
        with db.reader() as conn:
            return conn.execute(
                "SELECT COALESCE(SUM(msg_count), 0) FROM main.session_chan_signal WHERE chan_sub_id = ?",
                (sub,),
            ).fetchone()[0]

    assert signals() == 0
    assert _push(env, _args(account, "1", ts=now_ms, note=True, text="жив")) == 0
    assert signals() == 0
    assert _push(env, _args(account, "2", ts=now_ms, text="СТОП: причина")) == 0
    assert signals() == 1


def test_alert_is_for_me_and_react(env, capsys):
    """`--alert`: запись «лично владельцу» — у беседы-задачи (режим «к сведению») поднимается до «реагирую»."""
    db = env[0]
    account = _account(db)
    assert _push(env, _args(account, "1")) == 0
    assert _push(env, _args(account, "stop-1", alert=True, text="СТОП: капча")) == 0
    with db.reader() as conn:
        rows = conn.execute("SELECT ext_id, mentions_me FROM main.message ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("1", 0), ("stop-1", 1)]
    assert attention.message_attention("info", 0, "task") == "info"
    assert attention.message_attention("info", 1, "task") == "react"
    capsys.readouterr()
    assert _push(env, _args(account, "2", alert=True, note=True)) == 2
    assert "--note и --alert" in capsys.readouterr().err
