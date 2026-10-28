"""Ф57-1 (docs/architecture.md), ядро: режима «разобрать»
больше нет, письмо-рассылка — свой признак сообщения `bulk`.

- `message_attention`: `bulk` опускает `react` до `info` только в почтовой беседе; SQL-функция — 5 аргументов;
- приём: `message.bulk` пишется при вставке (своё письмо — никогда, правка признак не меняет), поле в выдаче;
- рассылка в `react`-почте не растит `unread_react` и не попадает в «Нужна реакция»; письмо человека — растит;
- П1: `bulk` читает ТОЛЬКО внимание — подписка «сообщения» сигналит по рассылке как раньше, отправитель
  рассылки не становится ботом;
- миграции 0085 (режим бесед, столбец) и 0086 (политика проектов);
- `triage` в API и CLI — отказ.

Обвязка подписок — `test_f26_multi_subs.py`."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub import cli as hub_cli
from hub.core import attention, ingest, queries, security
from hub.core import db as db_module
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret
from hub.core.db import Db
from hub.core.feed import Notifier

from .conftest import csrf_headers, insert_account, insert_channel, insert_project, login_via_code
from .test_f26_multi_subs import A, _msg_signals, _now, _sub, procs, world  # noqa: F401 — фикстуры

ADMIN_SECRET = ADMIN_PREFIX + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHI"
UI = security.Principal(token_id=0, kind="ui", scopes=frozenset({"read"}), project_ids=None)


def _mail(ext: str, ts: int, *, chat: str = "t1", bulk: bool = False, is_me: bool = False, **extra) -> dict:
    sender = {"ext_id": "me@x.ru", "is_me": True} if is_me else {"ext_id": "p@x.ru", "name": "Пётр"}
    event = {
        "type": "upsert",
        "channel": {"ext_id": chat, "kind": "mail_thread", "own_id_space": True, "title": "Тема"},
        "ext_id": ext,
        "ts": ts,
        "sender": sender,
        "is_me": is_me,
        "kind": "text",
        "text": "письмо",
        "mentions_me": not is_me,
        **extra,
    }
    if bulk:
        event["bulk"] = True
    return event


def _ingest(db, account_id: int, events: list[dict], now_ms: int = 10_000) -> None:
    result = db.write_sync(
        lambda conn: ingest.apply_batch(
            conn, account_id=account_id, events=events, cursor=None, now_ms=now_ms
        )
    )
    assert result.dead == 0, result


def _channel_id(db, ext: str) -> int:
    with db.reader() as conn:
        return conn.execute("SELECT id FROM main.channel WHERE ext_id = ?", (ext,)).fetchone()[0]


def _messages(db, channel_id: int) -> dict[str, dict]:
    with db.reader() as conn:
        page = queries.list_messages(conn, UI, channel_id, before=None, limit=200)
    return {m["ext_id"]: m for m in page.messages}


# --- внимание -----------------------------------------------------------------------------------------------


def test_message_attention_bulk_only_lowers_react_mail():
    assert attention.message_attention("react", 1, "mail_thread", 0, 1) == "info"
    assert attention.message_attention("react", 0, "mail_thread", 0, 1) == "info"
    assert attention.message_attention("react", 1, "mail_thread", 0, 0) == "react"
    # не почта — признак не действует (его ставит только коннектор почты; страховка от чужого события)
    assert attention.message_attention("react", 0, "dm", 0, 1) == "react"
    assert attention.message_attention("info", 1, "group", 0, 1) == "react"
    # прочие режимы — как были
    assert attention.message_attention("info", 1, "mail_thread", 0, 1) == "info"
    assert attention.message_attention("muted", 1, "mail_thread", 0, 1) == "muted"
    assert attention.message_attention(None, 1, "mail_thread", 0, 1) is None
    assert attention.MODES == ("react", "info")
    assert "triage" not in attention.MODES


def test_sql_function_takes_seven_arguments(db):
    # Ф63-1 П5: было 5 аргументов, стало 7 — ещё ручной режим беседы и внимание правила входящих.
    with db.reader() as conn:
        row = conn.execute(
            "SELECT hub_message_attention('react', 1, 'mail_thread', 0, 1, NULL, NULL), "
            "hub_message_attention('react', 1, 'mail_thread', 0, 0, NULL, NULL)"
        ).fetchone()
    assert tuple(row) == ("info", "react")
    assert ".bulk, " in attention.message_attention_sql()


# --- приём и счётчики ---------------------------------------------------------------------------------------


def test_bulk_written_on_insert_only_and_quiet_in_react_mail(db):
    acc = insert_account(db, source="mail")
    pid = insert_project(db, name="Umbrella")  # почта проекта — встроенное `react`
    _ingest(
        db,
        acc,
        [
            _mail("m1", 1000),
            _mail("m2", 2000, bulk=True),
            _mail("m3", 3000, bulk=True, is_me=True),  # своё письмо рассылкой не бывает
        ],
    )
    cid = _channel_id(db, "t1")
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.channel SET project_id = ? WHERE id = ?", (pid, cid))
    )
    with db.reader() as conn:
        rows = dict(conn.execute("SELECT ext_id, bulk FROM main.message").fetchall())
        bots = conn.execute("SELECT COUNT(*) FROM main.message WHERE is_bot = 1").fetchone()[0]
    assert rows == {"m1": 0, "m2": 1, "m3": 0}
    assert bots == 0  # П1: рассылка — не бот («Люди», подписки и напоминания читают `is_bot`)

    msgs = _messages(db, cid)
    assert (msgs["m1"]["bulk"], msgs["m1"]["attention"]) == (False, "react")
    assert (msgs["m2"]["bulk"], msgs["m2"]["attention"]) == (True, "info")
    assert msgs["m2"]["is_bot"] is False
    with db.reader() as conn:
        channel = queries.get_channel(conn, UI, cid)
        home = {row["channel"]["id"]: row for row in queries.home_react(conn, UI)}
    assert channel["attention"] == "react" and channel["unread_react"] == 1  # только письмо человека
    assert cid in home

    # правка и повторная сверка признак не меняют (как `is_bot`)
    _ingest(db, acc, [{**_mail("m1", 1000, bulk=True), "type": "edit", "edited_ts": 5000}])
    _ingest(db, acc, [_mail("m2", 2000)])
    with db.reader() as conn:
        assert dict(conn.execute("SELECT ext_id, bulk FROM main.message").fetchall()) == rows


def test_only_bulk_mail_gives_no_unread_react_and_no_home_row(db):
    acc = insert_account(db, source="mail")
    _ingest(db, acc, [_mail("m1", 1000, bulk=True), _mail("m2", 2000, bulk=True)])
    cid = _channel_id(db, "t1")  # без проекта: Ф57-1 — почта `react`
    with db.reader() as conn:
        channel = queries.get_channel(conn, UI, cid)
        home = {row["channel"]["id"] for row in queries.home_react(conn, UI)}
        changes = queries.changes_since(conn, UI, since=0, limit=100)
    assert channel["attention"] == "react"
    assert channel["unread_react"] == 0 and channel["unread"] == 2
    assert cid not in home
    assert [(m["bulk"], m["attention"]) for m in changes.messages] == [(True, "info"), (True, "info")]


def test_unsorted_modes_mail_react_chat_info_mention_raises(db):
    """Раздел 2.1: беседа без проекта — почта и лички `react`, остальное `info`; упоминание в чате
    поднимает."""
    tg = insert_account(db, source="telegram")
    mail = insert_account(db, source="mail")
    _ingest(db, mail, [_mail("m1", 1000)])
    group = {
        "type": "upsert",
        "channel": {"ext_id": "g1", "kind": "group", "title": "Группа"},
        "ts": 1000,
        "sender": {"ext_id": "7", "name": "Пётр"},
        "is_me": False,
        "kind": "text",
        "text": "x",
    }
    plain = {**group, "ext_id": "1", "mentions_me": False}
    mention = {**group, "ext_id": "2", "mentions_me": True}
    _ingest(db, tg, [plain, mention])
    mail_cid, group_cid = _channel_id(db, "t1"), _channel_id(db, "g1")
    with db.reader() as conn:
        assert queries.get_channel(conn, UI, mail_cid)["attention"] == "react"
        assert queries.get_channel(conn, UI, group_cid)["attention"] == "info"
        assert queries.get_channel(conn, UI, mail_cid)["unread_react"] == 1
        assert queries.get_channel(conn, UI, group_cid)["unread_react"] == 1
    msgs = _messages(db, group_cid)
    assert (msgs["1"]["attention"], msgs["2"]["attention"]) == ("info", "react")


def test_old_triage_value_in_data_does_not_break_and_reads_as_absent(db):
    """Раздел 2.1: строка, оставшаяся с `triage` (ручной режим, политика), расчёт не роняет."""
    acc = insert_account(db, source="slack")
    pid = insert_project(db, name="P")
    group = insert_channel(db, acc, project_id=pid, ext_id="C1", kind="group")
    dm = insert_channel(db, acc, project_id=pid, ext_id="D1", kind="dm")
    db.write_sync(lambda conn: conn.execute("UPDATE main.channel SET attention = 'triage'"))
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.project SET agent_policy_json = ? WHERE id = ?",
            (json.dumps({"attention": {"group": "triage", "default": "triage"}}), pid),
        )
    )
    with db.reader() as conn:
        got_group, got_dm = queries.get_channel(conn, UI, group), queries.get_channel(conn, UI, dm)
    assert (got_group["attention"], got_dm["attention"]) == ("info", "react")
    assert got_group["attention_manual"] in (None, "triage")  # сырое значение наружу роли не играет


# --- П1: признак читает только внимание --------------------------------------------------------------------


async def test_bulk_mail_still_signals_messages_subscription(client, db, world, procs, clock):  # noqa: F811
    """П1: письмо-рассылка в проекте с подпиской «сообщения» сигналит как раньше (подписки `bulk` не
    читают) — в отличие от сообщения бота, которое `chan_subs` отсеивает."""
    w = world
    mail = insert_account(db, source="mail")
    insert_channel(db, mail, project_id=w["pid"], ext_id="t1", kind="mail_thread")
    sub = (await _sub(client, w, A, mode="messages"))["sub"]["id"]
    now = _now(clock)
    _ingest(db, mail, [_mail("m1", now, bulk=True)], now)
    cid = _channel_id(db, "t1")
    signals = _msg_signals(db)
    assert [(s[1], s[2], s[3]) for s in signals] == [(sub, cid, "pending")]
    with db.reader() as conn:
        assert queries.get_channel(conn, UI, cid)["unread_react"] == 0  # а «реагирую» рассылка не даёт


# --- миграции ----------------------------------------------------------------------------------------------


@pytest.fixture
def stepped(monkeypatch):
    """`upto(N)` — миграции только до версии N включительно."""
    original = db_module._migrations

    def upto(version: int) -> None:
        monkeypatch.setattr(
            db_module,
            "_migrations",
            lambda *, main: {v: f for v, f in original(main=main).items() if v <= version},
        )

    return upto


POLICIES = {
    "full": '{"attention": {"mail_thread": "triage", "default": "triage", "group": "triage", '
    '"account:4": "triage", "dm": "triage", "mpim": "triage", "channel": "react", "account:5": "react"}, '
    '"digest_at": "10:00"}',
    "default_only": '{"attention": {"default": "triage"}, "other": [1, {"a": "triage"}]}',
    "clean": '{"attention": {"group": "info", "default": "digest"}, "x": 1}',
    "broken": "{битый",
    "attention_string": '{"attention": "triage"}',
    "not_object": "[1, 2]",
    "empty": "",
    "odd_values": '{"attention": {"group": null, "topic": 5, "card": "triage", "issue": {"a": "triage"}, '
    '"task": [1, "triage"], "dm": true, "channel": "a\\"b"}}',
}


def _next_seq(conn, schema: str) -> int:
    return conn.execute(f"SELECT COALESCE(MAX(seq), 0) + 1 FROM {schema}.message").fetchone()[0]  # noqa: S608


def test_migrations_0085_and_0086(tmp_path, stepped):
    stepped(84)
    old = Db(tmp_path)
    old.start()
    try:

        def fill(conn):
            ids = {}
            for name, policy in POLICIES.items():
                ids[name] = conn.execute(
                    "INSERT INTO main.project (name, agent_policy_json) VALUES (?, ?)", (name, policy)
                ).lastrowid
            ids["null"] = conn.execute("INSERT INTO main.project (name) VALUES ('null')").lastrowid
            for schema, source in (("main", "telegram"), ("work", "slack")):
                acc = conn.execute(
                    "INSERT INTO main.account (source, db) VALUES (?, ?)", (source, schema)
                ).lastrowid
                rows = [
                    ("dm", "dm", None, "triage"),
                    ("mail", "mail_thread", None, "triage"),
                    ("mpim", "group", '{"attrs": {"slack_type": "mpim"}}', "triage"),
                    ("group", "group", '{"attrs": {"slack_type": "public"}}', "triage"),
                    ("channel", "channel", None, "triage"),
                    ("card", "card", "{битый", "triage"),
                    ("keep_react", "group", None, "react"),
                    ("keep_digest", "mail_thread", None, "digest"),
                    ("keep_auto", "group", None, None),
                ]
                for ext, kind, meta, mode in rows:
                    chan = conn.execute(
                        f"INSERT INTO {schema}.channel (account_id, ext_id, kind, meta_json, attention) "  # noqa: S608
                        "VALUES (?, ?, ?, ?, ?)",
                        (acc, ext, kind, meta, mode),
                    ).lastrowid
                    for is_me in (0, 1):  # старые сообщения беседы: чужое и своё
                        seq = _next_seq(conn, schema)
                        conn.execute(
                            f"INSERT INTO {schema}.message (seq, first_seq, account_id, channel_id, ext_id, "  # noqa: S608
                            "is_me, kind, text, ts) VALUES (?, ?, ?, ?, ?, ?, 'text', 'x', 1)",
                            (seq, seq, acc, chan, f"{ext}-{is_me}", is_me),
                        )
                # почта проекта (не была в «разобрать») и почта без проекта без ручного режима
                for ext, project in (
                    ("mail_project", ids["clean"]),
                    ("mail_policy", ids["full"]),  # почта проекта, бывшая в «разобрать» по политике
                    ("mail_unsorted", None),
                ):
                    chan = conn.execute(
                        f"INSERT INTO {schema}.channel (account_id, ext_id, kind, project_id) "  # noqa: S608
                        "VALUES (?, ?, 'mail_thread', ?)",
                        (acc, ext, project),
                    ).lastrowid
                    seq = _next_seq(conn, schema)
                    conn.execute(
                        f"INSERT INTO {schema}.message (seq, first_seq, account_id, channel_id, "  # noqa: S608
                        "ext_id, is_me, kind, text, ts) VALUES (?, ?, ?, ?, ?, 0, 'text', 'x', 1)",
                        (seq, seq, acc, chan, f"{ext}-0"),
                    )
            return ids

        ids = old.write_sync(fill)
        with old.reader() as conn:
            assert conn.execute("PRAGMA main.user_version").fetchone()[0] == 84
            assert "bulk" not in {r[1] for r in conn.execute("PRAGMA main.table_info(message)")}
    finally:
        old.close()

    stepped(86)
    for _ in range(2):  # повторный запуск — без ошибок и без изменений
        new = Db(tmp_path)
        new.start()
        try:
            with new.reader() as conn:
                assert conn.execute("PRAGMA main.user_version").fetchone()[0] == 86
                assert conn.execute("PRAGMA work.user_version").fetchone()[0] == 85
                for schema in ("main", "work"):
                    cols = {r[1]: r for r in conn.execute(f"PRAGMA {schema}.table_info(message)")}
                    assert cols["bulk"][3] == 1 and cols["bulk"][4] == "0"  # NOT NULL DEFAULT 0
                    modes = dict(
                        conn.execute(f"SELECT ext_id, attention FROM {schema}.channel").fetchall()  # noqa: S608
                    )
                    assert modes == {
                        "dm": "react",
                        "mail": "react",
                        "mpim": "react",
                        "group": "info",
                        "channel": "info",
                        "card": "info",
                        "keep_react": "react",
                        "keep_digest": "digest",
                        "keep_auto": None,
                        "mail_project": None,
                        "mail_policy": None,
                        "mail_unsorted": None,
                    }, schema
                    # старая чужая почта бывших «разобрать» (по политике — разовый шаг ядра) — рассылка
                    marked = {
                        r[0]
                        for r in conn.execute(f"SELECT ext_id FROM {schema}.message WHERE bulk = 1")  # noqa: S608
                    }
                    assert marked == {"mail-0", "mail_unsorted-0", "mail_policy-0"}, schema
                got = dict(conn.execute("SELECT id, agent_policy_json FROM main.project").fetchall())
            # ключи вне `attention` целы; `default` и `account:<id>` с `triage` удалены, `mail_thread` не
            # добавляется; значения-строки остались строками (без двойного кодирования)
            assert json.loads(got[ids["full"]]) == {
                "attention": {
                    "mail_thread": "react",
                    "group": "info",
                    "dm": "react",
                    "mpim": "react",
                    "channel": "react",
                    "account:5": "react",
                },
                "digest_at": "10:00",
            }
            assert json.loads(got[ids["default_only"]]) == {"attention": {}, "other": [1, {"a": "triage"}]}
            assert json.loads(got[ids["odd_values"]]) == {
                "attention": {
                    "group": None,
                    "topic": 5,
                    "card": "info",
                    "issue": {"a": "triage"},
                    "task": [1, "triage"],
                    "dm": True,
                    "channel": 'a"b',
                }
            }
            # не тронуты байт в байт: без `triage`, битый JSON, `attention` строкой, не объект, пусто, NULL
            for name in ("clean", "broken", "attention_string", "not_object", "empty"):
                assert got[ids[name]] == POLICIES[name], name
            assert got[ids["null"]] is None
            # смигрированная политика читается расчётом режима
            assert attention.effective_mode(0, 0, None, 1, "mail_thread", None, got[ids["full"]]) == "react"
            assert attention.effective_mode(0, 0, None, 1, "group", None, got[ids["full"]], 4) == "info"
            assert attention.effective_mode(0, 0, None, 1, "mail_thread", None, got[ids["default_only"]]) == (
                "react"
            )
        finally:
            new.close()


def test_bulk_check_rejects_other_values(db):
    acc = insert_account(db, source="mail")
    _ingest(db, acc, [_mail("m1", 1000)])
    with pytest.raises(Exception, match="CHECK"):
        db.write_sync(lambda conn: conn.execute("UPDATE main.message SET bulk = 2"))


# --- запись режима: `triage` больше не принимается ---------------------------------------------------------


async def test_triage_rejected_by_public_api(client, db, clock):
    acc = insert_account(db, source="slack")
    cid = insert_channel(db, acc, project_id=None, ext_id="C1", kind="group")
    cookie, _ = await login_via_code(client, db, clock)
    r = await client.patch(
        f"/api/v1/channels/{cid}", json={"attention": "triage"}, headers=csrf_headers(cookie)
    )
    assert r.status_code == 422, r.text
    for mode in ("react", "info", None):
        r = await client.patch(
            f"/api/v1/channels/{cid}", json={"attention": mode}, headers=csrf_headers(cookie)
        )
        assert r.status_code == 200, r.text
    with db.reader() as conn:
        assert conn.execute("SELECT attention FROM main.channel").fetchone() == (None,)


async def test_triage_rejected_by_admin_socket(db):
    pid = insert_project(db, name="P")
    acc = insert_account(db, source="slack")
    cid = insert_channel(db, acc, project_id=pid, ext_id="C1", kind="group")
    app = create_internal_app(
        db, clock=lambda: 1_700_000_000.0, notifier=Notifier(), admin_key_hash=hash_secret(ADMIN_SECRET)
    )
    headers = {"Authorization": f"Bearer {ADMIN_SECRET}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://hub-internal") as c:
        r = await c.patch(f"/internal/v1/admin/channels/{cid}", json={"attention": "triage"}, headers=headers)
        assert r.status_code == 422, r.text
        for changes in ({"mail_thread": "triage"}, {"default": "triage"}, {"account:5": "triage"}):
            r = await c.patch(
                f"/internal/v1/admin/projects/{pid}", json={"attention": changes}, headers=headers
            )
            assert r.status_code == 422, (changes, r.text)
        ok = {"attention": {"mail_thread": "react"}}
        r = await c.patch(f"/internal/v1/admin/projects/{pid}", json=ok, headers=headers)
        assert r.status_code == 200 and r.json()["attention_policy"] == {"mail_thread": "react"}
    with db.reader() as conn:
        assert conn.execute("SELECT attention FROM main.channel").fetchone() == (None,)


def test_triage_rejected_by_cli_before_any_request(capsys):
    parser = hub_cli._build_parser()  # noqa: SLF001
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["channel", "attention", "5", "triage"])
    assert exc_info.value.code == 2
    assert parser.parse_args(["channel", "attention", "5", "info"]).mode == "info"
    capsys.readouterr()
    for argv in (["project", "set", "--help"], ["channel", "attention", "--help"]):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)
        # Ф86-1: `--digest {on,off}` — сводка дня после сжатия, не режим внимания Ф57.
        text = capsys.readouterr().out.replace("--digest {on,off}", "").replace("--digest", "")
        assert "digest" not in text and "triage" not in text, argv
    with pytest.raises(ValueError, match="неизвестный режим"):
        attention.normalize_policy_changes({"mail_thread": "triage"})
