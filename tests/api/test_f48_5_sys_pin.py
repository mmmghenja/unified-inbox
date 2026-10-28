"""Ф48-5 на уровне ядра (docs/architecture.md): снимок закреплённого системного текста
сессии хаба `hub_session.sys_pin`. Пишет только хост вместе с привязкой разговора движка; `pinned` в окне
«Инструкция роли» (`GET /sessions/{id}/role-text`); миграция 0071.

Хост поддельный (bearer `work_host`), как в test_f41_held_work.py."""

import hashlib
import json

from .test_f41_held_work import (
    API,
    _host,
    _mk,
    _rec,
    ui,  # noqa: F401 — фикстура
    world,  # noqa: F401 — фикстура
)
from .test_f48_3_role_file import PATH, _patch, _put, _text, _workdir

BODY = "# Роль\n\nтекст роли\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pin(**over) -> dict:
    return {
        "mode": "manual",
        "role": "тестер",
        "role_no": 1,
        "role_file": PATH,
        "file_sha": _sha(BODY),
        **over,
    }


async def _bind(client, w, sid, engine_sid="conv-1", *, expect=200, **extra):
    body = {"engine_session_id": engine_sid, "state": "asleep", **extra}
    return await _host(client, w, f"sessions/{sid}/bind", body, expect=expect)


async def _host_row(client, w, sid) -> dict:
    return {r["id"]: r for r in (await _host(client, w, "sessions"))["items"]}[sid]


# --- запись: только хост, вместе с привязкой ----------------------------------------------------------------


async def test_host_writes_pin_with_bind_and_only_host_sees_it(client, ui, db, world):  # noqa: F811
    w = world
    sid = await _mk(ui, w, role="тестер", role_file=PATH)
    assert _rec(db, sid).sys_pin is None
    assert "sys_pin" not in await _host_row(client, w, sid)  # снимка нет — запись хоста прежняя
    out = await _bind(client, w, sid, sys_pin=_pin())
    assert out["session"]["sys_pin"] == _pin()
    assert json.loads(_rec(db, sid).sys_pin) == _pin()
    assert (await _host_row(client, w, sid))["sys_pin"] == _pin()
    # Публичная форма и `/sync` снимка не несут.
    r = await ui.get(f"{API}/sessions/{sid}")
    assert "sys_pin" not in r.json()["session"]
    r = await ui.get(f"{API}/sync", params={"since": 0})
    assert all("sys_pin" not in s for s in r.json()["hub_sessions"])
    # Повтор той же привязки с тем же снимком ничего не двигает.
    seq = _rec(db, sid).seq
    await _bind(client, w, sid, sys_pin=_pin())
    assert _rec(db, sid).seq == seq
    # Привязка без снимка у того же разговора снимок не трогает.
    await _bind(client, w, sid)
    assert json.loads(_rec(db, sid).sys_pin) == _pin()
    # Тот же разговор, новый снимок (разговор у движка создан только сейчас) — заменяется.
    await _bind(client, w, sid, sys_pin=_pin(mode="auto"))
    assert json.loads(_rec(db, sid).sys_pin)["mode"] == "auto"
    assert _rec(db, sid).seq > seq


async def test_new_conversation_without_pin_clears_old_one(client, ui, db, world):  # noqa: F811
    w = world
    sid = await _mk(ui, w, role="тестер")
    await _bind(client, w, sid, sys_pin=_pin(role_file=None, file_sha=None))
    assert _rec(db, sid).sys_pin is not None
    await _bind(client, w, sid, "conv-2")  # другой разговор, снимка нет — прежний был про другой разговор
    assert _rec(db, sid).sys_pin is None and _rec(db, sid).engine_session_id == "conv-2"


async def test_pin_shape_is_checked_and_ui_cannot_write_it(client, ui, db, world):  # noqa: F811
    w = world
    sid = await _mk(ui, w, role="тестер")
    for bad in (
        _pin(mode="ручной"),
        _pin(file_sha="abc"),
        _pin(role_no=0),
        {**_pin(), "extra": 1},
        {"role": "тестер"},
        "строка",
    ):
        await _bind(client, w, sid, expect=422, sys_pin=bad)
    assert _rec(db, sid).sys_pin is None and _rec(db, sid).engine_session_id is None
    # Интерфейсу поля нет: ни при создании, ни правкой.
    await _patch(ui, sid, {"sys_pin": _pin()}, expect=422)
    r = await ui.c.post(
        f"{API}/sessions",
        json={"project_id": w["p3"], "engine": "claude", "sys_pin": _pin()},
        headers=ui.headers,
    )
    assert r.status_code == 422
    # Ручка привязки — только хосту.
    r = await ui.c.post(
        f"{API}/work/host/sessions/{sid}/bind",
        json={"engine_session_id": "x", "state": "asleep", "sys_pin": _pin()},
        headers=ui.headers,
    )
    assert r.status_code in (401, 403)
    assert _rec(db, sid).sys_pin is None


# --- окно «Инструкция роли» ---------------------------------------------------------------------------------


async def test_role_text_pinned(client, ui, db, world):  # noqa: F811
    w = world
    workdir = _workdir(db, w)
    _put(workdir, BODY)
    sid = await _mk(ui, w, role="тестер", role_file=PATH)
    assert (await _text(ui, sid))["pinned"] is None  # снимка нет
    await _bind(client, w, sid, sys_pin=_pin())
    assert (await _text(ui, sid))["pinned"] is True
    # Текст файла изменился.
    _put(workdir, BODY + "вторая редакция\n")
    assert (await _text(ui, sid))["pinned"] is False
    _put(workdir, BODY)
    assert (await _text(ui, sid))["pinned"] is True
    # Путь сменился (текст тот же).
    _put(workdir, BODY, "other/ROLE.md")
    await _patch(ui, sid, {"role_file": "other/ROLE.md"})
    assert (await _text(ui, sid))["pinned"] is False
    # Файл снят.
    await _patch(ui, sid, {"role_file": None})
    assert (await _text(ui, sid))["pinned"] is False
    # Закреплено без файла, файла и сейчас нет — совпадает.
    await _bind(client, w, sid, sys_pin=_pin(role_file=None, file_sha=None))
    assert (await _text(ui, sid))["pinned"] is True
    # Закреплён путь без текста (файл не читался), сейчас так же — совпадает; появился текст — нет.
    await _patch(ui, sid, {"role_file": "nope/ROLE.md"})
    await _bind(client, w, sid, sys_pin=_pin(role_file="nope/ROLE.md", file_sha=None))
    assert (await _text(ui, sid))["pinned"] is True
    _put(workdir, BODY, "nope/ROLE.md")
    assert (await _text(ui, sid))["pinned"] is False


async def test_role_text_pinned_is_null_for_opencode(client, ui, db, world):  # noqa: F811
    w = world
    _put(_workdir(db, w), BODY)
    sid = await _mk(ui, w, engine="opencode", role="тестер", role_file=PATH)
    await _bind(client, w, sid, "ses_1")
    assert (await _text(ui, sid))["pinned"] is None
    # Даже если снимок как-то оказался в записи — у OpenCode системный текст идёт с каждым ходом.
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.hub_session SET sys_pin = ? WHERE id = ?", (json.dumps(_pin()), sid)
        )
    )
    assert (await _text(ui, sid))["pinned"] is None


async def test_handoff_starts_without_pin(client, ui, db, world):  # noqa: F811
    w = world
    old = await _mk(ui, w, role="тестер", role_file=PATH)
    await _bind(client, w, old, sys_pin=_pin())
    new = (await ui.post(f"{API}/sessions/{old}/handoff"))["session"]
    assert new["id"] != old and _rec(db, new["id"]).sys_pin is None  # новый разговор — снимок напишет хост


# --- миграция -----------------------------------------------------------------------------------------------


def test_migration_0071_on_previous_version(tmp_path, monkeypatch):
    from hub.core import db as db_module
    from hub.core.db import Db

    def _cols(conn) -> set:
        return {r[1] for r in conn.execute("PRAGMA main.table_info(hub_session)")}

    original = db_module._migrations
    monkeypatch.setattr(
        db_module, "_migrations", lambda *, main: {v: f for v, f in original(main=main).items() if v <= 70}
    )
    old = Db(tmp_path)
    old.start()
    try:
        with old.reader() as conn:
            assert "sys_pin" not in _cols(conn)
    finally:
        old.close()
    monkeypatch.setattr(db_module, "_migrations", original)
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            assert conn.execute("PRAGMA main.user_version").fetchone()[0] >= 71
            assert "sys_pin" in _cols(conn)
            assert (
                conn.execute("SELECT COUNT(*) FROM main.hub_session WHERE sys_pin IS NOT NULL").fetchone()[0]
                == 0
            )
    finally:
        new.close()


async def test_common_sha_is_kept_and_null_is_not_stored(client, ui, db, world):  # noqa: F811
    """Ф56-1б (3.7): `common_sha` хранится в снимке; `null` не хранится — снимок как до 56-1б."""
    w = world
    sid = await _mk(ui, w, role="тестер", role_file=PATH)
    sha = "a" * 64
    await _bind(client, w, sid, sys_pin=_pin(common_sha=sha))
    assert json.loads(_rec(db, sid).sys_pin)["common_sha"] == sha
    other = await _mk(ui, w, role="тестер", role_file=PATH)
    await _bind(client, w, other, "conv-2", sys_pin=_pin(common_sha=None))
    assert json.loads(_rec(db, other).sys_pin) == _pin()
    await _bind(client, w, other, "conv-2", sys_pin=_pin(common_sha="nothex"), expect=422)
