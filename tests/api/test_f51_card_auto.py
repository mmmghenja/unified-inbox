"""Ф51-1 B (docs/architecture.md): ручка хоста «карточку закрыла снятая галочка автопилота» —
`POST /work/host/session-requests/{id}/auto`. Только хост; только из `pending`; ядро само сверяет, что сессия
в авто и ключ карточки снят. Ф66-1: делегата нет.

Помощники и «мир» — из test_hub_sessions.py и card_helpers.py; хост поддельный."""

from hub.core import held_work

from . import test_hub_sessions as base
from .card_helpers import API, PUSH, QUESTION, _card, _worker
from .test_hub_sessions import _audits, _h, _host

procs = base.procs
ui = base.ui
world = base.world

PUSH_RULE = {**PUSH, "rule": "push"}
ASK_RULE = {**QUESTION, "rule": "question"}
AUTO_BY = {"auto": True, "role": "автопилот"}


async def _free(ui, key: str, hold=False) -> None:  # noqa: F811
    r = await ui.put(f"{API}/autopilot", {"scope": "global", "key": key, "hold": hold})
    assert r.status_code == 200, r.text


def _row(db, card_id) -> dict:
    """Карточка, как её отдаёт ядро (и решённая: в записи сессии — только ждущие)."""
    with db.reader() as conn:
        return held_work.request_dict(held_work._request(conn, card_id)[0])  # noqa: SLF001


async def test_auto_closes_a_pending_card_under_a_lifted_switch(client, ui, db, world):  # noqa: F811
    w = world
    sid = await _worker(client, ui, w)
    card = (await _card(client, w, sid, PUSH_RULE))["id"]
    # ключ стоит — ядро не даёт закрыть карточку автопилотом
    await _host(client, w, f"session-requests/{card}/auto", {}, expect=409)
    assert _row(db, card)["status"] == "pending"

    await _free(ui, "push")
    out = await _host(client, w, f"session-requests/{card}/auto", {})
    assert out == {"id": card, "status": "allowed", "decided_by": AUTO_BY}
    row = _row(db, card)
    assert row["status"] == "allowed" and row["delivered"] is True
    assert row["decided_by"] == AUTO_BY and "delegate" not in row
    events = (await ui.get(f"{API}/sessions/{sid}/events")).json()
    [decision] = [e for e in events if e["kind"] == "decision"]
    assert decision["summary"] == "галочка снята — разрешено само: Bash"
    assert decision["brief"] == {"auto": True, "verdict": "allowed", "rule": "push"}
    audit = _audits(db, "hub_session_decide")[-1]
    assert audit["auto"] is True and audit["rule"] == "push" and audit["request_id"] == card
    # повтор и решение владельца после — «уже решено»
    await _host(client, w, f"session-requests/{card}/auto", {}, expect=409)
    r = await ui.post(f"{API}/session-requests/{card}/decide", {"decision": "deny"})
    assert r.status_code == 409


async def test_auto_question_is_recorded_as_host_answer(client, ui, db, world):  # noqa: F811
    w = world
    sid = await _worker(client, ui, w)
    card = (await _card(client, w, sid, ASK_RULE))["id"]
    await _free(ui, "question")
    out = await _host(client, w, f"session-requests/{card}/auto", {})
    assert out["status"] == "denied"  # на вопрос хост отвечает «решай сама»
    row = _row(db, card)
    assert row["status"] == "denied" and row["decided_by"] == AUTO_BY and row["delivered"] is True
    events = (await ui.get(f"{API}/sessions/{sid}/events")).json()
    assert [e["summary"] for e in events if e["kind"] == "decision"] == ["галочка снята — вопрос решает сама"]


async def test_auto_is_refused_without_rule_for_manual_and_after_owner(client, ui, db, world):  # noqa: F811
    w = world
    await _free(ui, "push")
    sid = await _worker(client, ui, w)
    # карточка без ключа — не автопилоту
    bare = (await _card(client, w, sid, PUSH))["id"]
    await _host(client, w, f"session-requests/{bare}/auto", {}, expect=409)
    # Владелец решил первым — его решение остаётся
    card = (await _card(client, w, sid, PUSH_RULE))["id"]
    r = await ui.post(f"{API}/session-requests/{card}/decide", {"decision": "deny"})
    assert r.status_code == 200, r.text
    await _host(client, w, f"session-requests/{card}/auto", {}, expect=409)
    row = _row(db, card)
    assert row["status"] == "denied" and row["decided_by"] is None
    # ручная сессия: выключатели к ней не применяются
    manual = await _worker(client, ui, w, role="ревьюер", mode="manual")
    card = (await _card(client, w, manual, PUSH_RULE))["id"]
    await _host(client, w, f"session-requests/{card}/auto", {}, expect=409)
    assert _row(db, card)["status"] == "pending"


async def test_auto_is_host_only(client, ui, db, world):  # noqa: F811
    w = world
    await _free(ui, "push")
    sid = await _worker(client, ui, w)
    card = (await _card(client, w, sid, PUSH_RULE))["id"]
    path = f"{API}/work/host/session-requests/{card}/auto"
    r = await client.post(path, json={}, headers=_h(w["tok"]))  # токен сессии проекта
    assert r.status_code in (401, 403)
    r = await ui.post(path, {})  # кука владельца
    assert r.status_code in (401, 403)
    r = await client.post(path, json={})
    assert r.status_code in (401, 403)
    assert _row(db, card)["status"] == "pending"
