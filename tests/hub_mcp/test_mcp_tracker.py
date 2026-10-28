"""Ф10-5 B4 (тест B-T5): беседа уведомлений трекера в hub-mcp — строка «трекер: …» внутри блока ДАННЫХ шапки
`hub_read`/`hub_thread`, без подсказки «Ответ: hub_draft(…)»; `hub_channels` — пометка «· трекер». Обычная
беседа — как было. Спецификация: docs/architecture.md."""

import json

from mcp import Client

from hub.mcp_server import format as fmt

from .conftest import insert_account, insert_channel, insert_message, insert_project, insert_thread, text_of
from .test_mcp_server import _blocks, _call, _profile

JIRA_ATTRS = {
    "tracker": "jira",
    "key": "WEB-39",
    "url": "https://acme.atlassian.net/browse/WEB-39",
    "no_reply": True,
}
TRACKER_LINE = (
    "трекер: Jira WEB-39 · https://acme.atlassian.net/browse/WEB-39 · "
    "ответ из хаба не уходит (комментарий — средствами трекера)"
)


def _set_attrs(db, channel_id: int, attrs: dict) -> None:
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET meta_json = ? WHERE id = ?",
            (json.dumps({"attrs": attrs, "own_id_space": True}), channel_id),
        )
    )


async def test_b_t5_tracker_header_inside_data_without_draft_hint(db, config_dir, make_server):
    pid = insert_project(db, name="Initech")
    acc = insert_account(db, source="mail")
    tracker = insert_channel(
        db,
        acc,
        project_id=pid,
        ext_id="jira:acme.atlassian.net:WEB-39",
        kind="issue",
        title="WEB-39 · Утилиты",
    )
    mail = insert_channel(db, acc, project_id=pid, ext_id="777", kind="mail_thread", title="письмо")
    _set_attrs(db, tracker, JIRA_ATTRS)
    insert_message(db, tracker, ext_id="<a@x>", ts=1000, text="Статус: READY → TEST")
    insert_message(db, tracker, ext_id="<b@x>", ts=2000, text="в треде", thread_ext_id="<a@x>")
    insert_thread(db, tracker, ext_id="<a@x>", last_reply_ts=2000)
    insert_message(db, mail, ext_id="<m@x>", ts=1000, text="привет")
    _profile(db, config_dir)
    server, _hub = make_server()
    async with Client(server) as client:
        read = text_of(await _call(client, "hub_read", channel_id=tracker))
        first_block = _blocks(read)[0][1]
        assert TRACKER_LINE in first_block
        head_outside = read.split("----- ДАННЫЕ", 1)[0]
        assert "трекер:" not in head_outside
        assert "hub_draft" not in read

        thread = text_of(await _call(client, "hub_thread", channel_id=tracker, thread="<a@x>"))
        assert TRACKER_LINE in _blocks(thread)[0][1]
        assert "hub_draft" not in thread

        plain = text_of(await _call(client, "hub_read", channel_id=mail))
        assert "Ответ: hub_draft(" in plain
        assert "трекер:" not in plain

        channels = text_of(await _call(client, "hub_channels", project_id=pid))
        line_tracker = next(line for line in channels.split("\n") if line.startswith(f"#{tracker} "))
        line_mail = next(line for line in channels.split("\n") if line.startswith(f"#{mail} "))
        assert " · трекер" in line_tracker
        assert " · трекер" not in line_mail


def test_b_t5_tracker_text_variants():
    assert fmt.tracker_text({"can_reply": True, "tracker": None}) is None
    assert fmt.tracker_text({}) is None  # ядро старее Ф10-5
    trello = {"name": "trello", "key": "Tr1CardA", "url": "https://trello.com/c/Tr1CardA"}
    assert fmt.tracker_text({"can_reply": False, "tracker": trello}) == (
        "трекер: Trello Tr1CardA · https://trello.com/c/Tr1CardA · "
        "ответ из хаба не уходит (комментарий — средствами трекера)"
    )
    assert fmt.tracker_text({"can_reply": False, "tracker": None}) == (
        "трекер: ответ из хаба не уходит (комментарий — средствами трекера)"
    )
