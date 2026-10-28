"""Ф48-3 на уровне ядра (docs/architecture.md): поле сессии `role_file` — путь к MD-файлу роли
относительно рабочей папки проекта. Создание и смена, 422 на негодный путь, три формы записи, предупреждение
«файл не прочитать», окно «Инструкция роли» (`GET /sessions/{id}/role-text`), передача дел, миграция 0069.

Хост поддельный (bearer `work_host`), как в test_f41_held_work.py."""

from pathlib import Path

import pytest

from hub import roles

from .test_f41_held_work import (
    API,
    _audits,
    _events,
    _get,
    _host,
    _mk,
    _rec,
    ui,  # noqa: F401 — фикстура
    world,  # noqa: F401 — фикстура
)

PATH = "docs/marketing/researcher/ROLE.md"
BAD = ["/etc/ROLE.md", "~/ROLE.md", "../ROLE.md", "docs/../../x.md", "ROLE.txt", "ROLE", "a" * 298 + ".md"]


async def _patch(ui, sid, body, *, expect=200) -> dict:  # noqa: F811
    r = await ui.c.patch(f"{API}/sessions/{sid}", json=body, headers=ui.headers)
    assert r.status_code == expect, r.text
    return r.json()


def _workdir(db, w, project="p3") -> Path:  # noqa: F811
    with db.reader() as conn:
        return Path(
            conn.execute("SELECT workdir FROM main.project WHERE id = ?", (w[project],)).fetchone()[0]
        )


def _put(workdir: Path, text="# Роль\n\nтекст роли\n", path=PATH) -> None:
    target = workdir / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


async def _text(ui, sid, *, expect=200) -> dict:  # noqa: F811
    r = await ui.get(f"{API}/sessions/{sid}/role-text")
    assert r.status_code == expect, r.text
    return r.json()


# --- создание и три формы записи ----------------------------------------------------------------------------


async def test_create_with_role_file_and_three_forms(client, ui, db, world):  # noqa: F811
    w = world
    _put(_workdir(db, w))
    seq0 = _rec(db, await _mk(ui, w, "p4")).seq
    out = await ui.post(
        f"{API}/sessions", {"project_id": w["p3"], "engine": "claude", "role": "тестер", "role_file": PATH}
    )
    assert out["session"]["role_file"] == PATH and "role_file_warning" not in out
    sid = out["session"]["id"]
    assert _rec(db, sid).role_file == PATH
    assert (await _get(ui, sid))["session"]["role_file"] == PATH  # публичная
    rows = {r["id"]: r for r in (await _host(client, w, "sessions"))["items"]}
    assert rows[sid]["role_file"] == PATH  # хосту — обязательно
    r = await ui.get(f"{API}/sync", params={"since": seq0})
    assert [s["role_file"] for s in r.json()["hub_sessions"] if s["id"] == sid] == [PATH]  # `/sync`
    assert _audits(db, "hub_session_create")[-1]["role_file"] is True
    # Без поля, `null` и пустая строка — файла нет (и в аудите создания ключа нет).
    for extra in ({}, {"role_file": None}, {"role_file": ""}, {"role_file": "   "}):
        other = (await ui.post(f"{API}/sessions", {"project_id": w["p4"], "engine": "claude", **extra}))[
            "session"
        ]
        assert other["role_file"] is None
        await ui.post(f"{API}/sessions/{other['id']}/close")
    assert "role_file" not in _audits(db, "hub_session_create")[-1]


@pytest.mark.parametrize("bad", BAD)
async def test_bad_path_is_422_with_text(ui, db, world, bad):  # noqa: F811
    w = world
    body = {"project_id": w["p3"], "engine": "claude", "role_file": bad}
    out = await ui.post(f"{API}/sessions", body, expect=422)
    assert "role_file" in str(out["detail"])
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.hub_session").fetchone()[0] == 0
    sid = await _mk(ui, w, role_file=PATH)
    seq = _rec(db, sid).seq
    out = await _patch(ui, sid, {"role_file": bad}, expect=422)
    assert "role_file" in str(out["detail"])
    assert (_rec(db, sid).role_file, _rec(db, sid).seq) == (PATH, seq)


async def test_patch_sets_and_clears(client, ui, db, world):  # noqa: F811
    w = world
    _put(_workdir(db, w))
    sid = await _mk(ui, w, role="тестер")
    seq0 = _rec(db, sid).seq
    out = await _patch(ui, sid, {"role_file": f"  {PATH} "})
    assert out["session"]["role_file"] == PATH and "role_file_warning" not in out
    seq1 = _rec(db, sid).seq
    assert seq1 > seq0
    last = (await _events(ui, sid))[-1]
    assert last["kind"] == "status" and last["brief"] == {"type": "role_file", "role_file": PATH}
    assert "со следующего запуска" in last["summary"]
    # То же значение — ничего не пишется.
    await _patch(ui, sid, {"role_file": PATH})
    assert _rec(db, sid).seq == seq1
    # Вместе с ролью одним запросом; пустая строка и `null` — снять.
    out = await _patch(ui, sid, {"role": "программист", "role_file": "ROLE.md"})
    assert (out["session"]["role"], out["session"]["role_file"]) == ("программист", "ROLE.md")
    assert (await _patch(ui, sid, {"role_file": ""}))["session"]["role_file"] is None
    assert (await _events(ui, sid))[-1]["brief"] == {"type": "role_file", "role_file": None}
    await _patch(ui, sid, {"role_file": PATH})
    assert (await _patch(ui, sid, {"role_file": None}))["session"]["role_file"] is None
    assert _audits(db, "hub_session_role_file") == [
        {"hub_session_id": sid, "old": False, "new": True},
        {"hub_session_id": sid, "old": True, "new": True},
        {"hub_session_id": sid, "old": True, "new": False},
        {"hub_session_id": sid, "old": False, "new": True},
        {"hub_session_id": sid, "old": True, "new": False},
    ]
    # Хосту смена закрыта; закрытая сессия — 409.
    r = await client.patch(
        f"{API}/sessions/{sid}", json={"role_file": PATH}, headers={"Authorization": f"Bearer {w['host']}"}
    )
    assert r.status_code == 403
    await ui.post(f"{API}/sessions/{sid}/close")
    await _patch(ui, sid, {"role_file": PATH}, expect=409)


async def test_warning_when_file_cannot_be_read(ui, db, world, tmp_path):  # noqa: F811
    w = world
    workdir = _workdir(db, w)
    # Файла ещё нет — запись принята, рядом предупреждение.
    out = await ui.post(f"{API}/sessions", {"project_id": w["p3"], "engine": "claude", "role_file": PATH})
    assert out["session"]["role_file"] == PATH
    assert out["role_file_warning"] == f"файл роли {PATH}: нет файла"
    sid = out["session"]["id"]
    # Симлинк наружу и большой файл — то же: запись принята, причина строкой.
    (tmp_path / "outside.md").write_text("СЕКРЕТ", encoding="utf-8")
    (workdir / "out.md").symlink_to(tmp_path / "outside.md")
    (workdir / "big.md").write_bytes(b"a" * (roles.FILE_MAX_BYTES + 1))
    out = await _patch(ui, sid, {"role_file": "out.md"})
    assert out["role_file_warning"] == "файл роли out.md: путь выходит за рабочую папку проекта"
    out = await _patch(ui, sid, {"role_file": "big.md"})
    assert out["role_file_warning"] == "файл роли big.md: файл больше 64 КиБ"
    # Файл появился — предупреждения нет; правка других полей о файле не говорит.
    _put(workdir)
    assert "role_file_warning" not in await _patch(ui, sid, {"role_file": PATH})
    (workdir / PATH).unlink()
    assert "role_file_warning" not in await _patch(ui, sid, {"mode": "auto"})
    assert "role_file_warning" not in await _patch(ui, sid, {"role_file": None})


# --- окно «Инструкция роли» ---------------------------------------------------------------------------------


async def test_role_text_reads_file_with_host_limits(client, ui, db, world, tmp_path):  # noqa: F811
    w = world
    workdir = _workdir(db, w)
    body = "# Роль\n\n  дословно, с отступом  \n"
    _put(workdir, body)
    sid = await _mk(ui, w, role="тестер", role_file=PATH)
    assert await _text(ui, sid) == {
        "role": "тестер 1",
        "common": roles.common_text(),
        "role_file": PATH,
        "file_text": body,
        "file_reason": None,
        "pinned": None,  # Ф48-5: снимка закреплённого текста нет
    }
    # Те же ограничения, что у хоста: нет файла, симлинк наружу, большой файл — причина, без содержимого.
    (tmp_path / "outside.md").write_text("СЕКРЕТ", encoding="utf-8")
    (workdir / "out.md").symlink_to(tmp_path / "outside.md")
    (workdir / "big.md").write_bytes(b"a" * (roles.FILE_MAX_BYTES + 1))
    for path, reason in (
        ("нет.md", "нет файла"),
        ("out.md", "путь выходит за рабочую папку проекта"),
        ("big.md", "файл больше 64 КиБ"),
    ):
        await _patch(ui, sid, {"role_file": path})
        got = await _text(ui, sid)
        assert (got["role_file"], got["file_text"], got["file_reason"]) == (path, None, reason)
        assert "СЕКРЕТ" not in str(got)
    # Путь наружу, попавший в запись мимо проверки ядра, тоже не читается.
    for bad in ("../outside.md", str(tmp_path / "outside.md")):
        db.write_sync(
            lambda conn, bad=bad: conn.execute(
                "UPDATE main.hub_session SET role_file = ? WHERE id = ?", (bad, sid)
            )
        )
        got = await _text(ui, sid)
        assert got["file_text"] is None and got["file_reason"].startswith("role_file:")
    # Файл не задан — только общий текст.
    await _patch(ui, sid, {"role_file": None})
    got = await _text(ui, sid)
    assert (got["role_file"], got["file_text"], got["file_reason"]) == (None, None, None)
    assert got["common"] == roles.common_text()
    # Только кука интерфейса; нет сессии — 404.
    for token in (w["host"], w["tok"]):
        r = await client.get(f"{API}/sessions/{sid}/role-text", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403
    await _text(ui, 999_999, expect=404)


async def test_builtin_role_texts_are_gone(ui):  # noqa: F811
    for key in ("duty", "programmer", "marketer"):
        r = await ui.get(f"{API}/roles/{key}")
        assert r.status_code == 200 and r.json()["text"] == roles.common_text()


# --- передача дел -------------------------------------------------------------------------------------------


async def test_handoff_carries_role_file(ui, db, world):  # noqa: F811
    w = world
    old = await _mk(ui, w, role="тестер", role_file=PATH)
    new = (await ui.post(f"{API}/sessions/{old}/handoff"))["session"]
    assert new["id"] != old and new["role_file"] == PATH and _rec(db, new["id"]).role_file == PATH
    plain = (await ui.post(f"{API}/sessions/{await _mk(ui, w, 'p4')}/handoff"))["session"]
    assert plain["role_file"] is None


# --- миграция -----------------------------------------------------------------------------------------------


def test_migration_0069_on_previous_version(tmp_path, monkeypatch):
    from hub.core import db as db_module
    from hub.core.db import Db

    def _cols(conn) -> set:
        return {r[1] for r in conn.execute("PRAGMA main.table_info(hub_session)")}

    original = db_module._migrations
    monkeypatch.setattr(
        db_module, "_migrations", lambda *, main: {v: f for v, f in original(main=main).items() if v <= 68}
    )
    old = Db(tmp_path)
    old.start()
    try:
        with old.reader() as conn:
            assert "role_file" not in _cols(conn)
    finally:
        old.close()
    monkeypatch.setattr(db_module, "_migrations", original)
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            assert conn.execute("PRAGMA main.user_version").fetchone()[0] >= 69
            assert "role_file" in _cols(conn)
            assert (
                conn.execute("SELECT COUNT(*) FROM main.hub_session WHERE role_file IS NOT NULL").fetchone()[
                    0
                ]
                == 0
            )
    finally:
        new.close()
