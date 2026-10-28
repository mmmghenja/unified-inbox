"""Ф37, правки по ревью кода (04.10.2026): закрепление трёх находок.

1. Авто-отправка сессии — не «Владелец ответил»: карточку чужого вопроса к нему она не закрывает (`ANSWERED_SQL`).
2. `hub_handled` принимает в `up_to` только чужое живое сообщение (не свою заготовку и не своё отправленное).
3. `hub_handled` закрывает карточки только до `up_to`.
"""

from hub.core import agent_items, handled, subs

from . import test_f37_autosend as S
from . import test_f37_session_handled as H
from .conftest import insert_channel, insert_message, insert_project
from .test_f37_autosend import procs, ui  # noqa: F401 — фикстуры

MIN = 60_000


def _statuses(db) -> dict[int, str]:
    with db.reader() as conn:
        return dict(conn.execute("SELECT id, status FROM main.agent_item").fetchall())


async def _unread(ui, ch) -> int:  # noqa: F811
    return (await ui.get(f"/api/v1/channels/{ch}")).json()["unread"]


async def _ready(client, db, tmp_path, clock) -> dict:
    d3 = tmp_path / "p3"
    d3.mkdir()
    p3 = insert_project(db, name="Umbrella", workdir=str(d3))
    db.write_sync(lambda conn: subs.set_project_subs(conn, p3, True, now_ms=0))
    db.write_sync(lambda conn: handled.set_project_enabled(conn, p3, True))
    acc = S._account(db)
    ch = insert_channel(db, acc, project_id=p3, ext_id="dm1", kind="dm", title="Партнёр")
    now = S._now(clock)
    first = insert_message(db, ch, ext_id="100", ts=now - 10 * MIN, text="автоответ")
    last = insert_message(db, ch, ext_id="101", ts=now - 5 * MIN, text="вопрос лично владельцу")
    token, token_id = S._token(db)
    w = {"p3": p3, "dir3": str(d3), "acc": acc, "ch": ch, "first": first, "last": last, "tok": token}
    w["tok_id"] = token_id
    await S._sub(client, w, S.A)
    return w


async def _mark(client, w, up_to):
    return await client.post(
        f"/api/v1/channels/{w['ch']}/handled",
        json={"up_to_message_id": up_to, "note": "автоответ, владелец не нужен", "session": S._sess(S.A)},
        headers=S._h(w["tok"]),
    )


async def test_handled_rejects_own_draft_placeholder_and_own_sent(client, ui, db, clock, tmp_path, procs):  # noqa: F811
    """Своя заготовка черновика стоит «сейчас по часам хаба»: отметка по ней прочитала бы входящее, которого
    сессия не видела. И своё отправленное — тоже не «последнее чужое, которое разобрал»."""
    w = await _ready(client, db, tmp_path, clock)
    placeholder = (await S._send(client, w, who=None)).json()["message_id"]
    r = await _mark(client, w, placeholder)
    assert r.status_code == 422 and "ЧУЖОЕ" in r.text, r.text
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.message SET ext_id = '500', ext_sort = 500 WHERE id = ?", (placeholder,)
        )
    )
    assert (await _mark(client, w, placeholder)).status_code == 422
    assert await _unread(ui, w["ch"]) == 2  # ничего не отмечено
    assert (await _mark(client, w, w["last"])).status_code == 200


async def test_handled_closes_cards_only_up_to(client, ui, db, clock, tmp_path, procs):  # noqa: F811
    w = await _ready(client, db, tmp_path, clock)
    card = H._card(db, w["ch"], w["last"], "new")  # карточка — на более новом сообщении
    assert (await _mark(client, w, w["first"])).status_code == 200
    assert await _unread(ui, w["ch"]) == 1  # `last` не разобрано
    assert _statuses(db)[card] == "new"
    assert (await _mark(client, w, w["last"])).status_code == 200
    assert _statuses(db)[card] == "done"


async def test_autosend_is_not_owner_answer_for_cards(client, ui, db, clock, tmp_path, procs):  # noqa: F811
    """П9 через карточки: ответ сессии одному участнику не закрывает карточку чужого вопроса к владельцу; обычный
    ответ владельца (строка без `auto_role`) — закрывает, как раньше."""
    w = await _ready(client, db, tmp_path, clock)
    await S._allow(ui, w["ch"])
    card = H._card(db, w["ch"], w["last"], "new")
    r = await S._send(client, w)
    assert r.json().get("auto") == {"role": S.ROLE}, r.text
    mid = r.json()["message_id"]
    delivered = "UPDATE main.message SET ext_id = '500', ext_sort = 500 WHERE id = ?"
    db.write_sync(lambda conn: conn.execute(delivered, (mid,)))
    assert await _unread(ui, w["ch"]) == 2  # П9: непрочитанное на месте
    assert db.write_sync(lambda conn: agent_items.close_answered(conn, w["ch"])) == []
    assert _statuses(db)[card] == "new"
    # Владелец ответил сам (своё сообщение без строки авто-отправки) — карточка отвечена
    insert_message(db, w["ch"], ext_id="600", ts=S._now(clock) + MIN, text="отвечаю сам", is_me=True)
    assert db.write_sync(lambda conn: agent_items.close_answered(conn, w["ch"])) == [card]
