"""Файлы в ответе по карточке: `files`
— id файлов, загруженных в сессию карточки; принимаются только репликой сессии (`turn` -> `reply`, карточка
состояния -> `say`/`interrupt`), с файлами текст может быть пустым; прочие виды и действия — 422.

Мир — test_hub_sessions.py; хост поддельный."""

from . import test_hub_sessions as base
from .test_f41_autopilot_5b import _auto
from .test_f41_session_files import PDF, _upload
from .test_f56_queue import _answer, _permission
from .test_hub_sessions import _host, _now
from .test_owner_state import _tool_event, _turn
from .test_owner_turn import ASK, _Turns

procs = base.procs
ui = base.ui
world = base.world


def _say_files(db, say_id) -> list[str]:
    with db.reader() as conn:
        return [
            r[0] for r in conn.execute("SELECT name FROM held_file WHERE say_id = ? ORDER BY name", (say_id,))
        ]


def _last_say(db, sid) -> tuple[int, str]:
    with db.reader() as conn:
        return conn.execute(
            "SELECT id, text FROM held_say WHERE session_id = ? ORDER BY id DESC LIMIT 1", (sid,)
        ).fetchone()


async def test_turn_reply_carries_files_without_text(client, ui, db, world):
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    turns = _Turns(client, w, sid)
    await turns.start()
    await turns.end(ASK)
    ref = f"turn:{sid}:1"
    f1 = (await _upload(ui, sid, PDF, "doc.pdf"))["id"]
    # «Принял» файлов не берёт.
    await _answer(client, ref, {"action": "ok", "files": [f1]}, ui=ui, expect=422)
    done = await _answer(client, ref, {"action": "reply", "files": [f1]}, ui=ui)
    assert done["status"] == "queued"
    say_id, text = _last_say(db, sid)
    assert text == "" and _say_files(db, say_id) == ["doc.pdf"]


async def test_turn_reply_with_foreign_file_is_422(client, ui, db, world):
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    turns = _Turns(client, w, sid)
    await turns.start()
    await turns.end(ASK)
    await _answer(
        client,
        f"turn:{sid}:1",
        {"action": "reply", "text": "держи", "files": ["f0123456789abcdef0123456789abcdef"]},
        ui=ui,
        expect=422,
    )


async def test_state_say_carries_files(client, ui, db, clock, world):
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    await _turn(client, w, sid, {"state": "running", "source": "self"})
    await _tool_event(client, w, sid, _now(clock))
    await _host(client, w, "sessions/recover", {})
    f1 = (await _upload(ui, sid, PDF, "log.pdf"))["id"]
    done = await _answer(
        client, f"broken:{sid}:0", {"action": "say", "text": "вот лог", "files": [f1]}, ui=ui
    )
    assert done["status"] == "queued"
    say_id, text = _last_say(db, sid)
    assert text == "вот лог" and _say_files(db, say_id) == ["log.pdf"]


async def test_files_on_permission_card_are_422(client, ui, db, world):
    w = world
    sid = await _auto(client, ui, w, mode="manual")
    num = await _permission(client, w, sid)
    f1 = (await _upload(ui, sid, PDF, "doc.pdf"))["id"]
    await _answer(client, f"req:{num}", {"action": "allow", "files": [f1]}, ui=ui, expect=422)
