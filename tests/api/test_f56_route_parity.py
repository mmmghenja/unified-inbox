"""Ф56-1а (docs/architecture.md–5): прежние ручки после выноса в ядро и новые поля.

- `POST /outbox/{id}/approve` и `/cancel` (тело — `outbox_ops`): прежние ответы, аудит, правка владельца пишется в
  `draft_text` и даёт исход `edited`, отмена — `declined`/`withdrawn`, отмена своего черновика сессией — без
  исхода;
- Ф66-1: `via_agent` сессии и `head_channel` / делегат проекта убраны — поле 422, ручки 404/405.

Мир и помощники — из test_hub_sessions.py и test_f41_autopilot_5b.py."""

import json

from . import test_hub_sessions as base
from .test_f41_autopilot_5b import _auto, _me, _send
from .test_f56_queue import _outbox, _outcomes, _patch_session
from .test_hub_sessions import _h

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"


def _route_audits(db, action, outbox_id) -> list[tuple]:
    """Запись ручки (не строка `outbox` из `approve_mode`): `(actor, result, detail)`."""
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT actor, result, detail_json FROM main.audit WHERE action = ? AND outbox_id = ? "
            "AND actor != 'outbox' ORDER BY id",
            (action, outbox_id),
        ).fetchall()
    return [(a, r, json.loads(d) if d else None) for a, r, d in rows]


async def test_ui_approve_and_cancel_unchanged(client, ui, db, clock, world):
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    edited = (await _send(client, w, w["ch"], _me(sid, None), text="исходный текст"))["outbox_id"]
    plain = (await _send(client, w, w["ch"], _me(sid, None), text="как есть"))["outbox_id"]
    dropped = (await _send(client, w, w["ch"], _me(sid, None), text="не надо"))["outbox_id"]
    taken_back = (await _send(client, w, w["ch"], _me(sid, None), text="передумал"))["outbox_id"]
    own = (await _send(client, w, w["ch"], _me(sid, None), text="сам отзову"))["outbox_id"]

    r = await ui.post(f"{API}/outbox/{edited}/approve", {"text": "исправленный текст"})
    assert r.status_code == 200 and r.json() == {"status": "queued", "paced": False, "not_before_ts": None}
    row = _outbox(db, edited)
    assert (row["status"], row["text"], row["draft_text"]) == (
        "queued",
        "исправленный текст",
        "исходный текст",
    )
    ((actor, result, detail),) = _route_audits(db, "outbox_approve", edited)
    assert (actor, result) == ("ui", "queued")
    assert (
        detail["text_len"] == len("исправленный текст")
        and detail["edited"] is True
        and "source" not in detail
    )

    r = await ui.post(f"{API}/outbox/{plain}/approve", {})
    assert r.status_code == 200, r.text
    ((actor, result, detail),) = _route_audits(db, "outbox_approve", plain)
    assert detail is None or ("text_len" not in detail and "source" not in detail)

    r = await ui.post(f"{API}/outbox/{dropped}/cancel")
    assert r.status_code == 200 and r.json() == {"status": "cancelled"}
    r = await ui.post(f"{API}/outbox/{taken_back}/approve", {})
    r = await ui.post(f"{API}/outbox/{taken_back}/cancel")
    assert r.status_code == 200 and r.json() == {"status": "cancelled"}
    assert _route_audits(db, "outbox_cancel", dropped) == [("ui", "cancelled", None)]
    # Сессия отзывает свой черновик — исхода нет.
    r = await client.post(f"{API}/outbox/{own}/cancel", headers=_h(w["tok"]))
    assert r.status_code == 200 and r.json() == {"status": "cancelled"}
    r = await ui.post(f"{API}/outbox/{own}/cancel")
    assert r.status_code == 409
    assert _outcomes(db) == [
        (edited, "edited"),
        (plain, "approved"),
        (dropped, "declined"),
        (taken_back, "approved"),
        (taken_back, "withdrawn"),
    ]
    r = await ui.post(f"{API}/outbox/999999/approve", {})
    assert r.status_code == 404


async def test_via_agent_and_delegate_handles_are_gone(client, ui, db, world):
    """Ф66-1 (D): «Канал владельца» у сессии, делегат и «головная закрывает проект» убраны."""
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    row = (await ui.get(f"{API}/sessions/{sid}")).json()
    assert "via_agent" not in (row.get("session") or row)
    await _patch_session(ui, sid, {"via_agent": True}, expect=422)
    for body in ({"project_id": w["p3"], "head_channel": True}, {"project_id": w["p3"], "role": "x"}):
        r = await ui.put(f"{API}/autopilot/delegate", body)
        assert r.status_code in (404, 405), r.text
    for path in ("/session-requests/delegated", "/session-requests/1/delegate-decide"):
        r = await client.post(f"{API}{path}", json={}, headers=_h(w["tok"], 7))
        assert r.status_code in (404, 405), (path, r.status_code)
    state = (await ui.get(f"{API}/autopilot")).json()
    assert "delegates" not in state and "delegate_wait_ms" not in state
    with db.reader() as conn:
        policy = conn.execute(
            "SELECT agent_policy_json FROM main.project WHERE id = ?", (w["p3"],)
        ).fetchone()[0]
    assert "card_delegate" not in json.loads(policy or "{}")
