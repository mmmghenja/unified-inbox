"""Ф46-1 П1 (`docs/architecture.md`): заготовка вида `tg_msg_author` в автопилот
не идёт никогда — при СНЯТОМ выключателе `hub_send_new` у сессии хаба в авто строка остаётся
`pending_approval`, текстом (`/drafts/new`) и с файлами (`hold` -> `/messages/send`). Рядом — контроль:
`@username` при тех же условиях уходит без подтверждения (условия остальных видов не тронуты).

Помощники и «мир» — из test_hub_sessions.py, test_f41_autopilot.py и test_f41_autopilot_5b.py."""

from . import test_hub_sessions as base
from .conftest import insert_channel, insert_message
from .test_f41_autopilot import _put
from .test_f41_autopilot_5b import API, _auto, _me, _outbox
from .test_hub_sessions import _audits, _h, _now

procs = base.procs
ui = base.ui
world = base.world

AUTHOR = 5550001


def _source(db, clock, w, *, author=AUTHOR, ext_id="812") -> int:
    """Сообщение человека в группе Telegram проекта p3 — id сообщения хаба."""
    with db.reader() as conn:
        row = conn.execute(
            "SELECT id FROM main.channel WHERE account_id = ? AND ext_id = '-1000000004242'", (w["acc"],)
        ).fetchone()
    group = (
        row[0]
        if row is not None
        else insert_channel(
            db, w["acc"], project_id=w["p3"], ext_id="-1000000004242", kind="group", title="Продавцы"
        )
    )
    contact = db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.contact (account_id, ext_id, name) VALUES (?, ?, 'Пенопласт')",
                (w["acc"], str(author)),
            ).lastrowid
        )
    )
    return insert_message(db, group, ext_id=ext_id, ts=_now(clock), sender_contact_id=contact)


async def _new(client, w, to, ident, **extra) -> dict:
    body = {"account_id": w["acc"], "project_id": w["p3"], "to": to, "text": "привет", **ident, **extra}
    r = await client.post(f"{API}/drafts/new", json=body, headers=_h(w["tok"]))
    assert r.status_code == 200, r.text
    return r.json()


async def _with_file(client, w, channel_id, ident, key) -> dict:
    up = await client.post(
        f"{API}/uploads?channel_id={channel_id}",
        content=b"%PDF-1.7\n" + key.encode() + b"x" * 100,
        headers={**_h(w["tok"]), "X-Filename": "a.pdf"},
    )
    assert up.status_code == 200, up.text
    body = {
        "channel_id": channel_id,
        "text": "во вложении",
        "idempotency_key": key,
        "files": [up.json()["upload_id"]],
        **ident,
    }
    r = await client.post(f"{API}/messages/send", json=body, headers=_h(w["tok"]))
    assert r.status_code == 200, r.text
    return r.json()


async def test_author_stub_text_never_goes_by_autopilot(client, ui, db, clock, world):  # noqa: F811
    w = world
    sid = await _auto(client, ui, w)
    await _put(ui, "global", "hub_send_new", False)
    message = _source(db, clock, w)

    kept = await _new(client, w, {"kind": "tg_msg_author", "message_id": message}, _me(sid))
    assert kept["status"] == "pending_approval" and not kept.get("auto")
    assert _outbox(db, kept["outbox_id"]) == ("pending_approval", None, None)
    assert _audits(db, "outbox_autosend") == []

    # Контроль: та же сессия, тот же выключатель — `@username` уходит без подтверждения.
    sent = await _new(client, w, "@control_one", _me(sid))
    assert sent["status"] == "queued" and sent["auto"]["by"] == "autopilot"


async def test_author_stub_with_files_never_goes_by_autopilot(client, ui, db, clock, world):  # noqa: F811
    w = world
    sid = await _auto(client, ui, w)
    await _put(ui, "global", "hub_send_new", False)
    message = _source(db, clock, w)

    held = await _new(
        client, w, {"kind": "tg_msg_author", "message_id": message}, {}, hold=True, text=None
    )
    kept = await _with_file(client, w, held["channel_id"], _me(sid), "f-author")
    assert kept["status"] == "pending_approval" and not kept.get("auto")
    assert _outbox(db, kept["outbox_id"]) == ("pending_approval", None, None)
    assert _audits(db, "outbox_autosend") == []

    # Контроль: заготовка `@username` с файлом при тех же условиях уходит сама.
    other = await _new(client, w, "@control_two", {}, hold=True, text=None)
    sent = await _with_file(client, w, other["channel_id"], _me(sid), "f-user")
    assert sent["status"] == "queued" and sent["auto"]["by"] == "autopilot"


async def test_session_draft_into_owners_author_stub_stays_draft(client, ui, db, clock, world):  # noqa: F811
    """Заготовку завёл владелец кликом (без `new_draft.to`, вид — в `attrs.new_to`), текст в неё кладёт сессия
    обычным `/messages/send` — тоже только черновик."""
    w = world
    sid = await _auto(client, ui, w)
    await _put(ui, "global", "hub_send_new", False)
    message = _source(db, clock, w)
    to = {"kind": "tg_msg_author", "message_id": message}
    r = await ui.post(f"{API}/channels/new", {"account_id": w["acc"], "project_id": w["p3"], "to": to})
    assert r.status_code == 200, r.text
    stub = r.json()["channel"]["id"]
    body = {"channel_id": stub, "text": "привет", "idempotency_key": "k-ui-stub", **_me(sid)}
    r = await client.post(f"{API}/messages/send", json=body, headers=_h(w["tok"]))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending_approval" and not r.json().get("auto")
    assert _audits(db, "outbox_autosend") == []
