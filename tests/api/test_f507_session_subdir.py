"""Ф50-7 на уровне ядра (docs/architecture.md, Р1–Р4, Р8, Р10 и поправки П1–П5): подпапка
сессии хаба — создание и три формы записи, отказы формы и диска (422), `PATCH` её не принимает, передача дел,
заявка агента с папкой и файлом роли (ключ совпадения, «Изменить и завести», путь «сразу», предупреждения),
подписка сессии хаба из подпапки.

Помощники и «мир» — из test_hub_sessions.py и test_f41_autopilot_5b.py; хост поддельный."""

import os
from pathlib import Path

import pytest

from hub.core import held

from . import test_hub_sessions as base
from .test_f41_autopilot import _put
from .test_f41_autopilot_5b import API, _ask, _auto, _me
from .test_hub_sessions import A, _audits, _host, _hsub, _mk, _sub

procs = base.procs
ui = base.ui
world = base.world

SUB = "proekty/z 1/кот"  # пробел и кириллица — по пути до процесса и гарда
ROLE_FILE = "roli/proekt.md"


def _rec(db, sid) -> held.HubSession:
    with db.reader() as conn:
        return held.get(conn, sid)


def _count(db) -> int:
    with db.reader() as conn:
        return conn.execute("SELECT COUNT(*) FROM main.hub_session").fetchone()[0]


def _tree(w, *subs) -> Path:
    root = Path(w["dir3"])
    for sub in subs:
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


async def _post(ui, path, body=None, *, expect=200) -> dict:  # noqa: F811
    r = await ui.post(path, body)
    assert r.status_code == expect, r.text
    return r.json()


# --- создание и три формы записи ----------------------------------------------------------------------------


async def test_create_with_subdir_and_three_forms(client, ui, db, world):  # noqa: F811
    w = world
    root = _tree(w, SUB)
    seq0 = (await _mk(ui, w, project="p4"))["seq"]
    made = await _mk(ui, w, role="тестер", subdir=f"./{SUB}/")
    sid = made["id"]
    assert (made["subdir"], made["workdir"], made["cwd"]) == (SUB, w["dir3"], str(root / SUB))
    assert (_rec(db, sid).subdir, _rec(db, sid).workdir) == (SUB, w["dir3"])
    hosted = {r["id"]: r for r in (await _host(client, w, "sessions"))["items"]}[sid]
    assert (hosted["subdir"], hosted["workdir"], hosted["cwd"]) == (SUB, w["dir3"], str(root / SUB))
    r = await ui.get(f"{API}/sync", params={"since": seq0})
    (synced,) = [s for s in r.json()["hub_sessions"] if s["id"] == sid]
    assert synced["subdir"] == SUB and "cwd" not in synced and "workdir" not in synced  # пути — не в ленту
    assert _audits(db, "hub_session_create")[-1]["subdir"] is True
    # Без поля, `null`, пустая строка и «.» — корень проекта: `cwd` — папка проекта, в аудите ключа нет.
    for extra in ({}, {"subdir": None}, {"subdir": ""}, {"subdir": "  "}, {"subdir": "."}, {"subdir": "./"}):
        other = await _mk(ui, w, project="p4", **extra)
        assert (other["subdir"], other["cwd"]) == (None, w["dir4"]) and other["workdir"] == w["dir4"]
        await ui.post(f"{API}/sessions/{other['id']}/close")
    assert "subdir" not in _audits(db, "hub_session_create")[-1]


@pytest.mark.parametrize(
    "bad", ["/abs", "~/x", "a/../b", "..", "a//b", ".git/hooks", "a/.claude", "a\\b", "a\nb", "x" * 301]
)
async def test_bad_form_is_422_with_text(ui, db, world, bad):  # noqa: F811
    out = await _mk(ui, world, expect=422, subdir=bad)
    assert "subdir" in str(out["detail"])
    assert _count(db) == 0


async def test_folder_on_disk_is_checked_at_create(ui, db, world, tmp_path):  # noqa: F811
    w = world
    root = _tree(w, "proekty/z1", "proekty/z5/.claude", "gruppa/zakaz")
    (root / "fail.txt").write_text("x")
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, root / "naruzhu")
    (root / "proekty/z5/.claude/settings.local.json").write_text("{}")
    (root / "gruppa/.mcp.json").write_text("{}")
    for subdir, text in (
        ("proekty/net", "папки proekty/net нет в проекте"),
        ("fail.txt", "fail.txt в проекте — не папка"),
        ("naruzhu", "папка naruzhu выходит за рабочую папку проекта"),
        # П4: свои настройки движка в самой папке и выше неё (до корня проекта).
        ("proekty/z5", "в папке свои настройки движка: proekty/z5/.claude/settings.local.json"),
        ("gruppa/zakaz", "в папке свои настройки движка: gruppa/.mcp.json"),
    ):
        out = await _mk(ui, w, expect=422, subdir=subdir)
        assert out["detail"] == text
    assert _count(db) == 0
    # Настройки в КОРНЕ проекта — не отказ (корень не в счёт): это настройки самого проекта.
    (root / ".mcp.json").write_text("{}")
    (root / ".claude").mkdir()
    (root / ".claude/settings.json").write_text("{}")
    assert (await _mk(ui, w, subdir="proekty/z1"))["subdir"] == "proekty/z1"


async def test_patch_does_not_take_subdir(ui, db, world):  # noqa: F811
    w = world
    _tree(w, "a", "b")
    sid = (await _mk(ui, w, subdir="a"))["id"]
    r = await ui.c.patch(f"{API}/sessions/{sid}", json={"subdir": "b"}, headers=ui.headers)
    assert r.status_code == 422, r.text
    assert _rec(db, sid).subdir == "a"


# --- передача дел (Р4, П5) ----------------------------------------------------------------------------------


async def test_handoff_copies_takes_and_checks_subdir(ui, db, world):  # noqa: F811
    w = world
    root = _tree(w, "a", "b", "c")
    old = (await _mk(ui, w, role="тестер", subdir="a"))["id"]
    new = (await _post(ui, f"{API}/sessions/{old}/handoff"))["session"]
    assert new["subdir"] == "a" and new["cwd"] == str(root / "a")  # нет ключа — папка прежней
    moved = (await _post(ui, f"{API}/sessions/{new['id']}/handoff", {"subdir": "b"}))["session"]
    assert moved["subdir"] == "b" and _rec(db, moved["id"]).subdir == "b"
    # Пустая строка и `null` — корень проекта (ключ задан), а не папка прежней.
    top = (await _post(ui, f"{API}/sessions/{moved['id']}/handoff", {"subdir": ""}))["session"]
    assert (top["subdir"], top["cwd"]) == (None, w["dir3"])
    back = (await _post(ui, f"{API}/sessions/{top['id']}/handoff", {"subdir": "c"}))["session"]
    # Прежняя папка пропала: по умолчанию — 422 и ничего не изменилось; с другой папкой — работает.
    (root / "c").rmdir()
    out = await _post(ui, f"{API}/sessions/{back['id']}/handoff", expect=422)
    assert out["detail"] == "папки c нет в проекте"
    assert _rec(db, back["id"]).closed_ts is None and _count(db) == 5
    (root / "b" / ".mcp.json").write_text("{}")
    out = await _post(ui, f"{API}/sessions/{back['id']}/handoff", {"subdir": "b"}, expect=422)
    assert out["detail"] == "в папке свои настройки движка: b/.mcp.json"  # П4 — и на передаче дел
    assert _rec(db, back["id"]).closed_ts is None
    last = (await _post(ui, f"{API}/sessions/{back['id']}/handoff", {"subdir": "a"}))["session"]
    assert last["subdir"] == "a" and _rec(db, back["id"]).close_reason == "handoff"


# --- заявка агента (Р8, П1, П2) -----------------------------------------------------------------------------


def _body(w, **extra) -> dict:
    return {"kind": "new_session", "project_id": w["p3"], "role": "Проект", "task": "веди заказ", **extra}


async def _requests(ui) -> dict:  # noqa: F811
    r = await ui.get(f"{API}/agent-requests")
    return {item["id"]: item for item in r.json()["requests"]}


async def test_request_carries_subdir_and_role_file_to_the_session(client, ui, db, clock, world):  # noqa: F811
    w = world
    root = _tree(w)
    sid = await _auto(client, ui, w, mode="manual")
    me = _me(sid)
    # Форма проверяется при подаче; существование — нет (агент создаст папку и файл к «Завести»).
    for bad in ({"subdir": "../x"}, {"subdir": "a/.git"}, {"role_file": "roli/proekt.txt"}):
        out = await _ask(client, w["tok"], _body(w, **me, **bad), expect=422)
        assert next(iter(bad)) in str(out["detail"])
    item = await _ask(client, w["tok"], _body(w, **me, subdir="proekty/z1/", role_file=ROLE_FILE))
    assert (item["payload"]["subdir"], item["payload"]["role_file"]) == ("proekty/z1", ROLE_FILE)
    assert item["subdir_warning"] == "папки proekty/z1 нет в проекте"
    assert item["role_file_warning"] == f"файл роли {ROLE_FILE}: нет файла"
    # П1: ключ совпадения — роль + папка. Та же роль в той же папке — та же заявка; в другой папке и без
    # папки — отдельные; без папки ключ прежний.
    same = await _ask(client, w["tok"], _body(w, **me, role="проект", subdir="proekty/z1"), expect=200)
    assert same["id"] == item["id"] and same["payload"]["role_file"] == ROLE_FILE
    other = await _ask(client, w["tok"], _body(w, **me, subdir="proekty/z2"))
    plain = await _ask(client, w["tok"], _body(w, **me))
    assert len({item["id"], other["id"], plain["id"]}) == 3
    assert (plain["payload"]["subdir"], plain["payload"]["role_file"]) == (None, None)
    assert "subdir_warning" not in plain and "role_file_warning" not in plain
    with db.reader() as conn:
        keys = dict(conn.execute("SELECT id, match_key FROM main.agent_request").fetchall())
    assert keys == {
        item["id"]: "role:проект|proekty/z1",
        other["id"]: "role:проект|proekty/z2",
        plain["id"]: "role:проект",
    }

    # «Завести», а папки нет — 422 владельцу тем же текстом, заявка остаётся ждать.
    out = await _post(ui, f"{API}/agent-requests/{item['id']}/approve", expect=422)
    assert out["detail"] == "папки proekty/z1 нет в проекте"
    assert (await _requests(ui))[item["id"]]["status"] == "pending"
    (root / "proekty/z1").mkdir(parents=True)
    (root / "proekty/z1/.mcp.json").write_text("{}")
    out = await _post(ui, f"{API}/agent-requests/{item['id']}/approve", expect=422)
    assert out["detail"] == "в папке свои настройки движка: proekty/z1/.mcp.json"
    assert (await _requests(ui))[item["id"]]["subdir_warning"] == out["detail"]
    (root / "proekty/z1/.mcp.json").unlink()
    (root / "roli").mkdir()
    (root / ROLE_FILE).write_text("# Роль\n")
    shown = (await _requests(ui))[item["id"]]
    assert "subdir_warning" not in shown and "role_file_warning" not in shown
    # Папка и файл появились — сессия несёт оба (раньше файл роли из заявки терялся).
    answer = await _post(ui, f"{API}/agent-requests/{item['id']}/approve")
    partner = answer["session"]
    assert (partner["subdir"], partner["role_file"]) == ("proekty/z1", ROLE_FILE)
    assert partner["cwd"] == str(root / "proekty/z1") and partner["workdir"] == w["dir3"]
    rec = _rec(db, partner["id"])
    assert (rec.subdir, rec.role_file, rec.created_by) == ("proekty/z1", ROLE_FILE, "request")
    assert "subdir_warning" not in answer["request"]  # у решённой заявки замечаний о диске нет
    audit = _audits(db, "agent_request_decide")[-1]
    assert (audit["subdir"], audit["role_file"]) == ("proekty/z1", ROLE_FILE)


async def test_approve_edits_subdir_and_role_file_by_key(client, ui, db, clock, world):  # noqa: F811
    """П2: «ключ есть» — пустая строка владельца значит корень и «без файла», к значению агента не откатывается."""
    w = world
    root = _tree(w, "proekty/z1", "proekty/z2", "roli")
    (root / ROLE_FILE).write_text("# Роль\n")
    (root / "roli/drugoy.md").write_text("# Другая\n")
    sid = await _auto(client, ui, w, mode="manual")
    asked = {"subdir": "proekty/z1", "role_file": ROLE_FILE}

    async def _made(role, body):
        item = await _ask(client, w["tok"], _body(w, **_me(sid), role=role, **asked))
        partner = (await _post(ui, f"{API}/agent-requests/{item['id']}/approve", body))["session"]
        return partner["subdir"], partner["role_file"]

    assert await _made("первый", {"subdir": "", "role_file": ""}) == (None, None)  # очистил
    assert await _made("второй", {"subdir": None, "role_file": None}) == (None, None)
    assert await _made("третий", {"subdir": "proekty/z2", "role_file": "roli/drugoy.md"}) == (
        "proekty/z2",
        "roli/drugoy.md",
    )
    assert await _made("четвёртый", {"engine": "claude"}) == ("proekty/z1", ROLE_FILE)  # ключей нет
    assert await _made("пятый", {"subdir": ""}) == (None, ROLE_FILE)  # каждый ключ — сам по себе
    # Негодная форма в правке владельца — 422, заявка ждёт.
    item = await _ask(client, w["tok"], _body(w, **_me(sid), role="шестой", **asked))
    out = await _post(ui, f"{API}/agent-requests/{item['id']}/approve", {"subdir": "../x"}, expect=422)
    assert "subdir" in str(out["detail"])
    assert (await _requests(ui))[item["id"]]["status"] == "pending"


async def test_auto_path_takes_subdir_and_role_file(client, ui, db, clock, world):  # noqa: F811
    """П3: выключатель снят — заявка исполняется сразу, папка и файл роли действуют; движок и режим — как у
    просителя (П13 Ф41-5 без изменений); папки нет — заявка остаётся ждать владельца."""
    w = world
    root = _tree(w, "proekty/z1", "roli")
    (root / ROLE_FILE).write_text("# Роль\n")
    sid = await _auto(client, ui, w)
    await _put(ui, "global", "new_session", False)
    asked = {"engine": "opencode", "mode": "manual", "subdir": "proekty/z1", "role_file": ROLE_FILE}
    auto = await _ask(client, w["tok"], _body(w, **_me(sid), **asked), expect=200)
    assert (auto["status"], auto["auto"]) == ("approved", True)
    with db.reader() as conn:
        (partner,) = [r for r in held.list_sessions(conn, project_id=w["p3"]) if r.created_by == "agent"]
    assert (partner.subdir, partner.role_file) == ("proekty/z1", ROLE_FILE)
    assert (partner.engine, partner.mode, partner.autosend_ok) == ("claude", "auto", 0)
    audit = _audits(db, "agent_request_auto")[-1]
    assert (audit["subdir"], audit["role_file"]) == ("proekty/z1", ROLE_FILE)
    # Папки нет — сразу не выходит: заявка ждёт владельца, сессии нет.
    before = _count(db)
    waits = await _ask(client, w["tok"], _body(w, **_me(sid), role="другой", subdir="proekty/net"))
    assert (waits["status"], waits["auto"]) == ("pending", False) and _count(db) == before
    assert waits["subdir_warning"] == "папки proekty/net нет в проекте"


# --- подписка (Р10) -----------------------------------------------------------------------------------------


async def test_hub_session_subscribes_from_subdir_and_external_does_not(client, ui, db, world):  # noqa: F811
    w = world
    root = _tree(w, SUB)
    sid = (await _mk(ui, w, role="тестер", subdir=SUB))["id"]
    # MCP-сервер сессии хаба шлёт `cwd` процесса — подпапку: проект берётся из записи, а не из папки.
    sub = (await _hsub(client, w, sid, cwd=str(root / SUB)))["sub"]
    assert sub["project_id"] == w["p3"]
    # Внешней сессии из той же подпапки — по-прежнему 403 (подпапка не проект).
    await _sub(client, w, A, cwd=str(root / SUB), role="внешний", expect=403)
