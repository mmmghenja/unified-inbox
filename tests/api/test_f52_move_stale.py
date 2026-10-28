"""Ф52-2 П8 (docs/architecture.md): заявка на перенос беседы устарела, если текущий
проект беседы уже не тот, из которого её просили перенести (`payload.from_project_id`), — а не только «уже в
целевом». Два проекта подали заявки на одну беседу из «Неразобранного», владелец одобрил обе — вторая беседу не
уводит.

Помощники и «мир» — из test_hub_sessions.py и test_f41_autopilot_5b.py; хост поддельный."""

import json

from hub.core import agent_requests

from . import test_hub_sessions as base
from .conftest import insert_channel
from .test_f41_autopilot_5b import API, _ask, _auto, _channel, _me
from .test_hub_sessions import _token

procs = base.procs
ui = base.ui
world = base.world


async def test_second_approved_request_does_not_take_the_channel_away(client, ui, db, clock, world):  # noqa: F811
    w = world
    org, _ = _token(db, name="mcp:org", scopes=("read", "draft", "attach", "organize"))
    chan = insert_channel(db, w["acc"], project_id=None, ext_id="lead", kind="dm", title="Заказчик")
    s3 = await _auto(client, ui, w, role="маркетолог", mode="manual", project="p3", token=org)
    s4 = await _auto(client, ui, w, role="менеджер", mode="manual", project="p4", token=org)

    body = {"kind": "move_channel", "channel_id": chan, "reason": "по нашей части"}
    first = await _ask(client, org, {**body, "project_id": w["p3"], **_me(s3, None)})
    second = await _ask(client, org, {**body, "project_id": w["p4"], **_me(s4, None)})
    assert first["id"] != second["id"]
    assert first["payload"]["from_project_id"] is None and second["payload"]["from_project_id"] is None

    done = (await ui.post(f"{API}/agent-requests/{first['id']}/approve")).json()["request"]
    assert done["status"] == "approved" and _channel(db, chan)[0] == w["p3"]
    # Вторая одобрена следом: беседа уже не в «Неразобранном» — заявка устарела, беседа остаётся в первом.
    stale = (await ui.post(f"{API}/agent-requests/{second['id']}/approve")).json()["request"]
    assert (stale["status"], stale["note"]) == ("declined", agent_requests.STALE_NOTE)
    assert _channel(db, chan)[0] == w["p3"]


async def test_request_without_from_project_keeps_the_old_rule(client, ui, db, clock, world):  # noqa: F811
    """Строки заявок, записанные до поля `from_project_id`, исполняются по-прежнему."""
    w = world
    org, _ = _token(db, name="mcp:org", scopes=("read", "draft", "attach", "organize"))
    sid = await _auto(client, ui, w, mode="manual", token=org)
    body = {"kind": "move_channel", "project_id": w["p4"], "channel_id": w["ch"], "reason": "туда"}
    item = await _ask(client, org, {**body, **_me(sid, None)})

    def _strip(conn):
        (raw,) = conn.execute(
            "SELECT payload_json FROM main.agent_request WHERE id = ?", (item["id"],)
        ).fetchone()
        payload = json.loads(raw)
        del payload["from_project_id"]
        conn.execute(
            "UPDATE main.agent_request SET payload_json = ? WHERE id = ?", (json.dumps(payload), item["id"])
        )

    db.write_sync(_strip)
    done = (await ui.post(f"{API}/agent-requests/{item['id']}/approve")).json()["request"]
    assert done["status"] == "approved" and _channel(db, w["ch"])[0] == w["p4"]
