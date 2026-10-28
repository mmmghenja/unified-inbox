"""Ф99-1 (docs/architecture.md и поправки П1–П4): сессия хаба из прошлого разговора Claude Code
папки проекта — список разговоров (`GET /projects/{id}/conversations`) и `POST /sessions` с `resume_conv`."""

import json
import os
import time
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import held, hub_sessions, security, subs

from .conftest import BASE_URL, csrf_headers, insert_project, login_via_code

API = "/api/v1"
ORIGIN = "http://127.0.0.1:8787"


def _slug(path: str) -> str:
    return "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in path)


def _conv(root, workdir, records, *, conv_id=None, age_s=600.0) -> str:
    conv_id = conv_id or str(uuid.uuid4())
    folder = root / "projects" / _slug(str(workdir))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{conv_id}.jsonl"
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))
    return conv_id


def _user(content, **extra) -> dict:
    return {"type": "user", "message": {"role": "user", "content": content}, **extra}


@pytest.fixture
def claude_root(tmp_path, monkeypatch):
    root = tmp_path / "claude"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    return root


@pytest.fixture
def world(db, tmp_path):
    workdir = tmp_path / "Family"
    workdir.mkdir()
    (workdir / "sub").mkdir()
    pid = insert_project(db, name="Личное", workdir=str(workdir))
    db.write_sync(lambda conn: subs.set_project_subs(conn, pid, True, now_ms=0))
    return {"pid": pid, "workdir": workdir}


@pytest.fixture
async def ui(app, db, clock):
    clock.value = time.time()  # подсказка «менялся недавно» — по часам ядра против mtime файла
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as c:
        cookie, _csrf = await login_via_code(c, db, clock)
        c.headers.update(csrf_headers(cookie))
        yield c


async def _create(ui, world, expect=200, **body):
    body.setdefault("engine", "claude")
    r = await ui.post(f"{API}/sessions", json={"project_id": world["pid"], **body})
    assert r.status_code == expect, r.text
    return r.json()


# --- список разговоров --------------------------------------------------------------------------------------


async def test_list_skips_service_records_masks_and_sorts(ui, world, claude_root):
    secret = "a1" * 20  # длинный hex — под маску
    long_text = "Продолжим про дачу " + "слово " * 60
    old = _conv(
        claude_root,
        world["workdir"],
        [
            {"type": "summary", "summary": "x"},
            _user("<command-name>/clear</command-name>"),
            _user("служебное", isMeta=True),
            _user("<local-command-stdout>ок</local-command-stdout>"),
            _user([{"type": "tool_result", "tool_use_id": "t", "content": "вывод"}]),
            _user([{"type": "text", "text": f"Привет, токен {secret} — разберись"}]),
            _user("вторая реплика"),
        ],
        age_s=3600,
    )
    fresh = _conv(claude_root, world["workdir"], [_user(long_text)], age_s=60)
    empty = _conv(claude_root, world["workdir"], [{"type": "assistant", "message": {}}], age_s=7200)
    # Чужая папка и не UUID — мимо списка.
    _conv(claude_root, world["workdir"] / "sub", [_user("из подпапки")])
    folder = claude_root / "projects" / _slug(str(world["workdir"]))
    (folder / "agent-1.jsonl").write_text(json.dumps(_user("подагент")) + "\n", encoding="utf-8")

    r = await ui.get(f"{API}/projects/{world['pid']}/conversations")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["id"] for i in items] == [fresh, old, empty]  # свежие сверху
    by_id = {i["id"]: i for i in items}
    assert by_id[old]["preview"] == "Привет, токен *** — разберись"
    assert secret not in json.dumps(items)
    assert len(by_id[fresh]["preview"]) == 160 and by_id[fresh]["preview"].endswith("…")
    assert by_id[empty]["preview"] == ""
    assert all(i["hub_session_id"] is None for i in items)
    assert by_id[old]["size"] == (folder / f"{old}.jsonl").stat().st_size
    assert abs(by_id[old]["last_ts"] - int((time.time() - 3600) * 1000)) < 5000


async def test_list_marks_bound_and_is_ui_only(ui, client, db, world, claude_root):
    conv = _conv(claude_root, world["workdir"], [_user("про дачу")])
    session = (await _create(ui, world, resume_conv=conv))["session"]
    items = (await ui.get(f"{API}/projects/{world['pid']}/conversations")).json()["items"]
    assert [(i["id"], i["hub_session_id"]) for i in items] == [(conv, session["id"])]
    # Закрыта — разговор снова свободен.
    assert (await ui.post(f"{API}/sessions/{session['id']}/close", json={})).status_code == 200
    items = (await ui.get(f"{API}/projects/{world['pid']}/conversations")).json()["items"]
    assert items[0]["hub_session_id"] is None
    # Bearer (даже `read` на все проекты) — нельзя: только кука интерфейса.
    created = db.write_sync(
        lambda conn: security.create_api_token(
            conn, name="mcp:all", kind="interactive", scopes=["read"], project_ids=None, expires_ts=None
        )
    )
    headers = {"Authorization": f"Bearer {created.token}", "Origin": ORIGIN}
    r = await client.get(f"{API}/projects/{world['pid']}/conversations", headers=headers)
    assert r.status_code == 403, r.text
    assert (await ui.get(f"{API}/projects/999/conversations")).status_code == 404


# --- создание сессии из разговора ---------------------------------------------------------------------------


async def test_resume_conv_binds_conversation_with_import_marker(ui, db, world, claude_root):
    conv = _conv(claude_root, world["workdir"], [_user("про дачу")])
    reply = await _create(ui, world, resume_conv=conv)
    assert "resume_warning" not in reply  # разговор не менялся 10 минут
    with db.reader() as conn:
        rec = held.get(conn, reply["session"]["id"])
    assert rec.engine_session_id == conv and rec.state == "asleep"
    assert json.loads(rec.sys_pin) == hub_sessions.IMPORTED_PIN
    # Файл роли с пометкой вместо снимка — «закреплён ли» неизвестно, а не «изменён».
    assert hub_sessions.role_file_pinned(rec, "текст") is None


async def test_resume_conv_fresh_gives_hint_not_refusal(ui, world, claude_root):
    conv = _conv(claude_root, world["workdir"], [_user("про дачу")], age_s=30)
    reply = await _create(ui, world, resume_conv=conv)
    assert reply["session"]["id"]
    assert "закрой его" in reply["resume_warning"]


async def test_resume_conv_refusals(ui, world, claude_root):
    conv = _conv(claude_root, world["workdir"], [_user("про дачу")])
    for body, status, words in (
        ({"engine": "opencode"}, 422, "только на движке Claude Code"),
        ({"subdir": "sub"}, 422, "без подпапки"),
    ):
        r = await _create(ui, world, expect=status, resume_conv=conv, **body)
        assert words in r["detail"], r
    r = await _create(ui, world, expect=422, resume_conv="не-uuid")
    assert "UUID" in r["detail"]
    r = await _create(ui, world, expect=422, resume_conv=str(uuid.uuid4()))
    assert "нет" in r["detail"]
    # Разговор из другой папки (подпапка проекта) в папке проекта не найден.
    other = _conv(claude_root, world["workdir"] / "sub", [_user("из подпапки")])
    await _create(ui, world, expect=422, resume_conv=other)
    first = (await _create(ui, world, resume_conv=conv))["session"]
    r = await _create(ui, world, expect=409, resume_conv=conv)
    assert f"#{first['id']}" in r["detail"]


async def test_imported_flag_survives_bind_and_not_handoff(ui, client, db, world, claude_root):
    conv = _conv(claude_root, world["workdir"], [_user("про дачу")])
    sid = (await _create(ui, world, resume_conv=conv))["session"]["id"]
    created = db.write_sync(
        lambda conn: security.create_api_token(
            conn,
            name="work:host",
            kind="background",
            scopes=["work_host", "read"],
            project_ids=None,
            expires_ts=None,
        )
    )
    host = {"Authorization": f"Bearer {created.token}", "Origin": ORIGIN}
    pin = {
        "mode": "manual",
        "role": None,
        "role_no": None,
        "role_file": None,
        "file_sha": None,
        "imported": True,
    }
    r = await client.post(
        f"{API}/work/host/sessions/{sid}/bind",
        json={"engine_session_id": conv, "state": "asleep", "sys_pin": pin},
        headers=host,
    )
    assert r.status_code == 200, r.text
    assert r.json()["session"]["sys_pin"] == pin  # признак хранится в снимке
    with db.reader() as conn:
        rec = held.get(conn, sid)
    assert hub_sessions.pin_imported(hub_sessions.sys_pin_of(rec)) is False  # уже настоящий снимок
    assert hub_sessions.role_file_pinned(rec, None) is True
    # `imported: false` в снимке не хранится.
    r = await client.post(
        f"{API}/work/host/sessions/{sid}/bind",
        json={"engine_session_id": conv, "state": "asleep", "sys_pin": {**pin, "imported": False}},
        headers=host,
    )
    assert "imported" not in r.json()["session"]["sys_pin"]
    # «Новая с передачей дел» — новый разговор: ни разговора, ни признака.
    r = await ui.post(f"{API}/sessions/{sid}/handoff", json={})
    assert r.status_code == 200, r.text
    new_id = r.json()["session"]["id"]
    with db.reader() as conn:
        new = held.get(conn, new_id)
    assert new.engine_session_id is None and new.sys_pin is None
