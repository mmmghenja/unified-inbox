"""Ф77-1 (docs/architecture.md): машина у карточек очереди владельца (`machine_name`) и у
записи сессии хаба (`machine` — всегда машина ядра).

Мир — test_hub_sessions.py; машина ядра — `HUB_HOST_ID=CORE` до сборки приложения, внешняя сессия A шлёт
`host=LAPTOP` и поле `machine`."""

import pytest

from hub import hostid

from . import test_hub_sessions as base
from .test_f41_autopilot_5b import _auto
from .test_f56_queue import _queue, _refs
from .test_hub_sessions import ROLE, A, _h, _hs

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"
CORE = "c0" * 8
LAPTOP = "1a" * 8


@pytest.fixture(autouse=True)
def core_host(monkeypatch):
    monkeypatch.setenv(hostid.ENV, CORE)


def _far(cwd=None) -> dict:
    body = {"pid": A[0], "proc_start": A[1], "session_id": A[2], "host": LAPTOP}
    if cwd is not None:
        body.update({"cwd": cwd, "kind": "interactive", "name": None, "name_source": None})
    return body


async def _to_owner(client, ui, w, session, role=ROLE) -> int:
    r = await ui.post(f"{API}/peer-msgs", {"text": "как дела?", "to": {"project_id": w["p3"], "role": role}})
    assert r.status_code == 201, r.text
    r = await client.post(
        f"{API}/peer-msgs",
        json={"text": "готово", "reply_to": r.json()["msg"]["id"], "session": session},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 201, r.text
    return r.json()["msg"]["id"]


async def test_hub_session_card_and_record_on_core_machine(client, ui, world):
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    msg_id = await _to_owner(client, ui, w, _hs(sid))
    card = _refs(await _queue(client, ui=ui))[f"agent_msg:{msg_id}"]
    rows = (await ui.get(f"{API}/sessions")).json()["items"]
    row = next(r for r in rows if r["id"] == sid)
    assert row["machine"]["host"] == CORE and row["machine"]["core"] is True
    assert card["machine_name"] == row["machine"]["display"]


async def test_external_session_card_names_its_machine(client, ui, world):
    w = world
    body = {
        "session": _far(cwd=w["dir3"]),
        "role": ROLE,
        "machine": {"hostname": "laptop", "route": "s1", "version": None},
    }
    r = await client.post(f"{API}/subs", json=body, headers=_h(w["tok"]))
    assert r.status_code == 200, r.text
    msg_id = await _to_owner(client, ui, w, _far())
    card = _refs(await _queue(client, ui=ui))[f"agent_msg:{msg_id}"]
    assert card["session_id"] is None and card["machine_name"] == "laptop"
