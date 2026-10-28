"""Файлы в разговоре «Мой агент».

Владелец загружает файл (`POST /owner_channel/talk/files`, только кука) и прикладывает его к реплике (`files` у
`POST /owner_channel/talk`, текст может быть пустым); строки ленты и `open` агента несут `files`; байты приложенного
отдаются владельцу и агенту (`GET /owner_channel/talk/files/{id}`: картинка — `inline` со своим типом, прочее —
`attachment`), неприложенного — 404; агент файлов не прикладывает; чистка — 24 ч неприложенным, 30 дней
приложенным, байты общего склада живут, пока на них ссылается файл сессии."""

import hashlib

from hub.core import held_files

from . import test_hub_sessions as base
from .test_f41_autopilot_5b import _auto
from .test_f41_session_files import PDF, PNG, _upload
from .test_f56_queue import _agent
from .test_hub_sessions import _h, _token
from .test_owner_talk import API, _feed, _open, _say

procs = base.procs
ui = base.ui
world = base.world

DAY = 24 * 60 * 60 * 1000


async def _up(ui, data, name="a.bin", *, expect=200, content=None):
    r = await ui.c.post(
        f"{API}/owner_channel/talk/files",
        params={"name": name},
        content=data if content is None else content,
        headers=ui.headers,
    )
    assert r.status_code == expect, r.text
    return r.json()


async def test_reply_carries_files_and_agent_reads_bytes(client, ui, db):
    agent, _ = _agent(db)
    shot = await _up(ui, PNG, "экран.png")
    doc = await _up(ui, PDF, "../../отчёт.pdf")
    assert shot["kind"] == "image" and shot["mime"] == "image/png"
    assert doc["name"] == "отчёт.pdf" and doc["kind"] == "pdf"
    # Неприложенный файл никому не отдаётся.
    assert (await ui.get(f"{API}/owner_channel/talk/files/{shot['id']}")).status_code == 404

    mine = await _say(client, {"text": "", "files": [doc["id"], shot["id"]]}, ui=ui)
    assert mine["text"] == ""
    assert [f["name"] for f in mine["files"]] == ["экран.png", "отчёт.pdf"]  # порядок загрузки
    assert all(f["gone"] is False for f in mine["files"])
    assert [f["id"] for f in (await _feed(client, ui=ui))["items"][-1]["files"]] == [shot["id"], doc["id"]]
    assert [f["id"] for f in (await _open(client, agent))[-1]["files"]] == [shot["id"], doc["id"]]

    r = await client.get(f"{API}/owner_channel/talk/files/{shot['id']}", headers=_h(agent))
    assert r.status_code == 200 and r.content == PNG
    assert r.headers["content-type"] == "image/png" and r.headers["content-disposition"] == "inline"
    assert r.headers["x-content-type-options"] == "nosniff"
    r = await ui.get(f"{API}/owner_channel/talk/files/{doc['id']}")
    assert r.status_code == 200 and r.content == PDF
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"].startswith("attachment;")

    # Второй раз тот же файл не прикладывается, удалить приложенный нельзя.
    await _say(client, {"text": "ещё", "files": [shot["id"]]}, ui=ui, expect=422)
    r = await ui.c.delete(f"{API}/owner_channel/talk/files/{shot['id']}", headers=ui.headers)
    assert r.status_code == 409


async def test_rights_and_refusals(client, ui, db):
    agent, _ = _agent(db)
    other, _ = _token(db)
    f1 = await _up(ui, PDF, "a.pdf")
    # Загружает и убирает только кука; агент файлов не прикладывает.
    for token in (agent, other):
        r = await client.post(
            f"{API}/owner_channel/talk/files", params={"name": "x.pdf"}, content=PDF, headers=_h(token)
        )
        assert r.status_code == 403
        r = await client.delete(f"{API}/owner_channel/talk/files/{f1['id']}", headers=_h(token))
        assert r.status_code == 403
    await _say(client, {"text": "держи", "files": [f1["id"]]}, token=agent, expect=422)
    mine = await _say(client, {"text": "держи", "files": [f1["id"]]}, ui=ui)
    r = await client.get(f"{API}/owner_channel/talk/files/{mine['files'][0]['id']}", headers=_h(other))
    assert r.status_code == 403
    # Пусто без файлов, чужой id, пустое тело.
    await _say(client, {"text": "  "}, ui=ui, expect=422)
    await _say(client, {"text": "", "files": ["f0123456789abcdef0123456789abcdef"]}, ui=ui, expect=422)
    await _up(ui, b"", "пусто.txt", expect=422)


async def test_remove_pending(client, ui, db):
    f1 = await _up(ui, PDF, "a.pdf")
    r = await ui.c.delete(f"{API}/owner_channel/talk/files/{f1['id']}", headers=ui.headers)
    assert r.status_code == 200
    r = await ui.c.delete(f"{API}/owner_channel/talk/files/{f1['id']}", headers=ui.headers)
    assert r.status_code == 404
    await _say(client, {"text": "", "files": [f1["id"]]}, ui=ui, expect=422)
    assert not held_files.file_path(db.data_dir, _sha(PDF)).exists()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def test_cleanup_ttls(client, ui, db, clock):
    pending = await _up(ui, PNG, "p.png")
    kept = await _up(ui, PDF, "k.pdf")
    mine = await _say(client, {"text": "вот", "files": [kept["id"]]}, ui=ui)
    now = int(clock() * 1000)
    await db.write(lambda conn: held_files.cleanup(conn, now_ms=now + DAY + 1))
    assert not held_files.file_path(db.data_dir, _sha(PNG)).exists()
    assert held_files.file_path(db.data_dir, _sha(PDF)).exists()
    await _say(client, {"text": "", "files": [pending["id"]]}, ui=ui, expect=422)
    await db.write(lambda conn: held_files.cleanup(conn, now_ms=now + 31 * DAY))
    assert not held_files.file_path(db.data_dir, _sha(PDF)).exists()
    item = next(i for i in (await _feed(client, ui=ui))["items"] if i["id"] == mine["id"])
    assert item["files"][0]["name"] == "k.pdf" and item["files"][0]["gone"] is True
    assert (await ui.get(f"{API}/owner_channel/talk/files/{kept['id']}")).status_code == 404


async def test_shared_bytes_survive_session_file_purge(client, ui, db, world):
    """Тот же PNG загружен в разговор и убран из сессии — байты разговора на месте (правило ссылок)."""
    sid = await _auto(client, ui, world, mode="manual")
    held = await _upload(ui, sid, PNG, "s.png")
    talk = await _up(ui, PNG, "t.png")
    r = await ui.c.delete(f"{API}/sessions/{sid}/files/{held['id']}", headers=ui.headers)
    assert r.status_code == 200
    assert held_files.file_path(db.data_dir, _sha(PNG)).exists()
    await _say(client, {"text": "", "files": [talk["id"]]}, ui=ui)
    r = await ui.get(f"{API}/owner_channel/talk/files/{talk['id']}")
    assert r.status_code == 200 and r.content == PNG
