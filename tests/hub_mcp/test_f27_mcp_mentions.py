"""hub-mcp, Ф27-1 B (`docs/architecture.md`): описание `hub_draft` — как упомянуть человека
Slack; предупреждение о `<@U…>`, которого нет в справочнике аккаунта беседы; Slack-id у людей в `hub_people` /
`hub_person`."""

from mcp import Client

from hub.core import ingest, people, people_stats

from .conftest import insert_account, insert_channel, insert_project, text_of
from .test_mcp_server import _call, _profile

NOW_MS = 1_700_000_000_000
WARN = "— не человек воркспейса, уйдёт текстом."


def _publish_people(db, account_id, items):
    events = [
        {"type": "account_meta", "key": "slack_people:0", "value": items},
        {"type": "account_meta", "key": "slack_people", "value": {"parts": 1, "count": len(items)}},
    ]
    db.write_sync(
        lambda conn: ingest.apply_batch(
            conn, account_id=account_id, events=events, cursor=None, now_ms=NOW_MS
        )
    )


async def test_draft_description_tells_how_to_mention(db, config_dir, make_server):
    _profile(db, config_dir)
    server, _hub = make_server()
    async with Client(server) as client:
        tools = {t.name: t.description for t in (await client.list_tools()).tools}
    assert "<@U…>" in tools["hub_draft"] and "@channel/@here хаб не ставит" in tools["hub_draft"]


async def test_draft_warns_about_unknown_slack_id_only(db, config_dir, make_server):
    project = insert_project(db, name="Globex")
    slack = insert_account(db, source="slack")
    tg = insert_account(db, source="telegram")
    _publish_people(db, slack, [{"id": "U0AAA", "real_name": "Анна", "display_name": None}])
    ch = insert_channel(db, slack, project_id=project, ext_id="C1", kind="group", title="general")
    tg_ch = insert_channel(db, tg, project_id=project, ext_id="100", title="Пётр")
    _profile(db, config_dir, project_ids=[project])
    server, _hub = make_server()
    async with Client(server) as client:
        out = text_of(
            await _call(client, "hub_draft", channel_id=ch, text="<@U0AAA>, <@U0ZZZ> и <@W0YYY> <@U0ZZZ>")
        )
        assert "Черновик #" in out
        assert f"<@U0ZZZ> {WARN}" in out and f"<@W0YYY> {WARN}" in out
        assert out.count(f"<@U0ZZZ> {WARN}") == 1
        assert "<@U0AAA> —" not in out

        clean = text_of(await _call(client, "hub_draft", channel_id=ch, text="<@U0AAA> глянь"))
        assert WARN not in clean
        # не Slack — проверки нет
        tg_out = text_of(await _call(client, "hub_draft", channel_id=tg_ch, text="<@U0ZZZ>"))
        assert WARN not in tg_out


def _person_world(db):
    project = insert_project(db, name="Globex")

    def _fn(conn):
        acc = conn.execute("INSERT INTO main.account (source, db) VALUES ('slack', 'main')").lastrowid
        kira = conn.execute(
            "INSERT INTO main.contact (account_id, ext_id, name, handle) "
            "VALUES (?, 'U0KIRA', 'Кира', '@kira')",
            (acc,),
        ).lastrowid
        dm = conn.execute(
            "INSERT INTO main.channel (account_id, ext_id, kind, project_id, title) "
            "VALUES (?, 'D1', 'dm', ?, 'Кира')",
            (acc, project),
        ).lastrowid
        for i, (sender, me) in enumerate([(kira, 0), (None, 1)]):
            seq = conn.execute(
                "UPDATE seq_counter SET value = value + 1 WHERE id = 1 RETURNING value"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO main.message (seq, first_seq, account_id, channel_id, ext_id, ts, "
                "sender_contact_id, is_me, kind, text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'text', 'привет')",
                (seq, seq, acc, dm, f"m{i}", NOW_MS - 1000 + i, sender, me),
            )
        people.link_all(conn, NOW_MS)
        return kira

    kira = db.write_sync(_fn)
    people_stats.recompute(db, NOW_MS)
    with db.reader() as conn:
        person = conn.execute("SELECT person_id FROM main.contact WHERE id = ?", (kira,)).fetchone()[0]
    return project, person


async def test_people_and_person_show_slack_id(db, config_dir, make_server):
    project, person = _person_world(db)
    _profile(db, config_dir, project_ids=[project])
    server, _hub = make_server()
    async with Client(server) as client:
        listing = text_of(await _call(client, "hub_people", query="кира", known_only=False))
        assert "Slack @kira U0KIRA" in listing
        card = text_of(await _call(client, "hub_person", person_id=person))
        assert "id U0KIRA (упомянуть: <@U0KIRA>)" in card
