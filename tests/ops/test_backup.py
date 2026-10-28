"""Тесты 2-5 (кусок 7): бэкап — полный круг (`VACUUM INTO` -> `age` -> расшифровка), отсутствие recipient,
поддельный `age` (падает / зависает дольше таймаута), ретенция, параллельный запуск. Спецификация:
docs/architecture.md
"""

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from hub import backup_recipient
from hub.core import backup as backup_mod

from .conftest import admin_headers, insert_account, insert_channel, insert_message

_HAS_AGE = shutil.which("age") is not None and shutil.which("age-keygen") is not None
_needs_age = pytest.mark.skipif(not _HAS_AGE, reason="age/age-keygen не установлены")

_MARKER_TEXT = "hub-backup-roundtrip-marker-please-survive-me"


def _generate_recipient(tmp_path: Path) -> tuple[str, Path]:
    """`age-keygen` во временный каталог. Возвращает (recipient, путь к identity-файлу с приватным ключом)."""
    identity_path = tmp_path / "identity.txt"
    r = subprocess.run(  # noqa: S603, S607
        ["age-keygen", "-o", str(identity_path)], check=True, capture_output=True, text=True, timeout=30
    )
    # "Public key: age1..." -- age-keygen пишет её в stderr.
    line = next(ln for ln in r.stderr.splitlines() if "age1" in ln)
    recipient = line.split("age1", 1)[1]
    return "age1" + recipient.strip(), identity_path


def _seed_marker_message(db, clock) -> None:
    account_id = insert_account(db, source="fake")
    channel_id = insert_channel(db, account_id)
    insert_message(db, channel_id, ext_id="1", ts=int(clock() * 1000), text=_MARKER_TEXT)


# --- тест 2: полный круг ------------------------------------------------------------------------------


@_needs_age
async def test_backup_roundtrip_decrypts_and_passes_integrity_check(
    internal_client, db, clock, config_dir, tmp_path
) -> None:
    recipient, identity_path = _generate_recipient(tmp_path)
    backup_recipient.write_recipient(config_dir, recipient)
    _seed_marker_message(db, clock)

    r = await internal_client.post("/internal/v1/admin/backup")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert {f["name"] for f in body["files"]} == {"main", "work"}
    assert body["kept"] == 1
    assert body["removed"] == 0

    backups_dir = db.data_dir / "backups"
    age_files = sorted(backups_dir.glob("*.age"))
    assert len(age_files) == 2
    # backups/ верхнего уровня не содержит ничего, кроме .age (плюс служебный .work, не файл).
    assert all(p.name.endswith(".age") for p in backups_dir.iterdir() if p.is_file())

    main_age = next(p for p in age_files if p.name.startswith("main-"))
    decrypted = tmp_path / "decrypted-main.db"
    subprocess.run(  # noqa: S603, S607, ASYNC221 -- тестовая расшифровка, блокировать некого
        ["age", "-d", "-i", str(identity_path), "-o", str(decrypted), str(main_age)],
        check=True,
        capture_output=True,
        timeout=30,
    )

    conn = sqlite3.connect(decrypted)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        row = conn.execute("SELECT text FROM message WHERE text = ?", (_MARKER_TEXT,)).fetchone()
        assert row is not None
    finally:
        conn.close()


# --- тест 3: нет recipient, поддельный age (падает / зависает) ---------------------------------------


async def test_no_recipient_is_409_and_backups_dir_stays_empty(internal_client, db) -> None:
    r = await internal_client.post("/internal/v1/admin/backup")
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "backup recipient not configured"
    assert not (db.data_dir / "backups").exists()


def _write_fake_age(path: Path, script: str) -> None:
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


async def test_fake_age_exit_1_is_500_and_leaves_no_partial_set(
    internal_client, db, config_dir, tmp_path, monkeypatch
) -> None:
    backup_recipient.write_recipient(config_dir, "age1" + "x" * 58)

    # Первый вызов (main) "успешен" (просто копирует вход в выход, без реального шифрования — тест не
    # проверяет содержимое), второй (work) падает -- имитирует комплект, развалившийся посередине.
    state_file = tmp_path / "fake-age-called"
    fake_age = tmp_path / "age"
    _write_fake_age(
        fake_age,
        f"""#!/bin/sh
set -e
out=""
in=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -r) shift 2 ;;
    *) in="$1"; shift ;;
  esac
done
if [ -f "{state_file}" ]; then
  exit 1
else
  touch "{state_file}"
  cp "$in" "$out"
  exit 0
fi
""",
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")

    r = await internal_client.post("/internal/v1/admin/backup")
    assert r.status_code == 500, r.text

    backups_dir = db.data_dir / "backups"
    assert list(backups_dir.rglob("*.tmp")) == []
    assert list(backups_dir.glob("*.age")) == []


async def test_vacuum_sqlite_error_on_second_file_is_500_and_leaves_no_partial_set(
    internal_client, db, config_dir, tmp_path, monkeypatch
) -> None:
    """Поправка архитектора: `sqlite3.Error` из VACUUM — тот же путь уборки, что и сбой `age` (раньше
    ловились только OSError/ошибки subprocess, и уже зашифрованный main оставался неполным комплектом)."""
    backup_recipient.write_recipient(config_dir, "age1" + "x" * 58)
    fake_age = tmp_path / "age"
    _write_fake_age(
        fake_age,
        """#!/bin/sh
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -r) shift 2 ;;
    *) in="$1"; shift ;;
  esac
done
cp "$in" "$out"
""",
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")

    real_vacuum = backup_mod._vacuum_into  # noqa: SLF001
    calls = []

    def _vacuum(src_path, dest_path):
        calls.append(src_path)
        if len(calls) == 2:
            raise sqlite3.OperationalError("disk I/O error")
        real_vacuum(src_path, dest_path)

    monkeypatch.setattr(backup_mod, "_vacuum_into", _vacuum)

    r = await internal_client.post("/internal/v1/admin/backup")
    assert r.status_code == 500, r.text
    assert r.json() == {"detail": "backup failed"}

    backups_dir = db.data_dir / "backups"
    assert list(backups_dir.rglob("*.tmp")) == []
    assert list(backups_dir.glob("*.age")) == []

    # Причина сбоя — в audit (вид сбоя, без путей): по голому 500 чинить было бы не по чему.
    with db.reader() as conn:
        detail = conn.execute(
            "SELECT detail_json FROM main.audit WHERE action = 'backup' AND result = 'error'"
        ).fetchone()[0]
    assert json.loads(detail) == {"reason": "OperationalError", "waited_ms": 0}

    # Замок отпущен: следующий бэкап проходит.
    monkeypatch.setattr(backup_mod, "_vacuum_into", real_vacuum)
    r = await internal_client.post("/internal/v1/admin/backup")
    assert r.status_code == 200, r.text


async def test_fake_age_hangs_past_timeout_is_500_and_leaves_no_tmp(
    internal_client, db, config_dir, tmp_path, monkeypatch
) -> None:
    backup_recipient.write_recipient(config_dir, "age1" + "x" * 58)

    fake_age = tmp_path / "age"
    _write_fake_age(fake_age, "#!/bin/sh\nsleep 60\n")

    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr(backup_mod, "AGE_TIMEOUT_S", 0.3)

    r = await internal_client.post("/internal/v1/admin/backup")
    assert r.status_code == 500, r.text

    backups_dir = db.data_dir / "backups"
    assert list(backups_dir.rglob("*.tmp")) == []
    assert list(backups_dir.glob("*.age")) == []


# --- тест 4: ретенция ----------------------------------------------------------------------------------


@_needs_age
async def test_retention_keeps_7_newest_complete_sets(
    internal_client, db, clock, config_dir, tmp_path
) -> None:
    recipient, _identity_path = _generate_recipient(tmp_path)
    backup_recipient.write_recipient(config_dir, recipient)

    labels: list[str] = []
    for _ in range(9):
        r = await internal_client.post("/internal/v1/admin/backup")
        assert r.status_code == 200, r.text
        labels.append(backup_mod._timestamp_label(int(clock() * 1000)))  # noqa: SLF001 -- тест сверяет метки
        clock.advance_ms(2_000)  # достаточно, чтобы метка (секунды) сдвинулась

    backups_dir = db.data_dir / "backups"
    sets = backup_mod._scan_sets(backups_dir)  # noqa: SLF001
    assert len(sets) == 7
    assert all(names == {"main", "work"} for names in sets.values())

    kept_labels = set(sets)
    removed_labels = set(labels) - kept_labels
    assert len(removed_labels) == 2
    # Удалены именно САМЫЕ старые -- две первые метки прогона.
    assert removed_labels == set(labels[:2])
    assert kept_labels == set(labels[2:])


# --- тест 5: параллельный второй бэкап + запись во время бэкапа ---------------------------------------


async def test_concurrent_backup_is_409_and_writes_go_through(
    internal_client, db, config_dir, tmp_path, monkeypatch
) -> None:
    backup_recipient.write_recipient(config_dir, "age1" + "x" * 58)

    fake_age = tmp_path / "age"
    _write_fake_age(
        fake_age,
        """#!/bin/sh
sleep 0.5
out=""
in=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -r) shift 2 ;;
    *) in="$1"; shift ;;
  esac
done
cp "$in" "$out"
""",
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")

    async def _second_backup() -> int:
        await asyncio.sleep(0)  # дать первому запросу стартовать первым
        r = await internal_client.post("/internal/v1/admin/backup")
        return r.status_code

    async def _write_during_backup() -> int:
        await asyncio.sleep(0)
        r = await internal_client.post(
            "/internal/v1/admin/projects", headers=admin_headers(), json={"name": "во время бэкапа"}
        )
        return r.status_code

    first_task = asyncio.ensure_future(internal_client.post("/internal/v1/admin/backup"))
    await asyncio.sleep(0)

    second_status, write_status = await asyncio.gather(_second_backup(), _write_during_backup())
    first_response = await first_task

    # Замок берётся уже в потоке `to_thread` — кто из двух возьмёт его первым, не гарантировано; инвариант —
    # ровно один бэкап прошёл, второй получил 409.
    assert sorted([first_response.status_code, second_status]) == [200, 409], first_response.text
    assert write_status == 200


# --- поправки после opus-критика куска 7 --------------------------------------------------------------

_COPY_AGE = """#!/bin/sh
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -r) shift 2 ;;
    *) in="$1"; shift ;;
  esac
done
cp "$in" "$out"
"""


def _install_copy_age(tmp_path: Path, monkeypatch) -> None:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _write_fake_age(bin_dir / "age", _COPY_AGE)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")


async def test_clock_jump_backwards_keeps_the_fresh_set(
    internal_client, db, clock, config_dir, tmp_path, monkeypatch
) -> None:
    """Находка 3: ретенция сортирует по метке имени — без защиты текущего прогона скачок часов назад удалял
    только что сделанный комплект при ответе `ok`."""
    backup_recipient.write_recipient(config_dir, "age1" + "x" * 58)
    _install_copy_age(tmp_path, monkeypatch)

    for _ in range(7):
        r = await internal_client.post("/internal/v1/admin/backup")
        assert r.status_code == 200, r.text
        clock.advance_ms(2_000)

    clock.advance_ms(-10 * 24 * 3600 * 1000)
    fresh_label = backup_mod._timestamp_label(int(clock() * 1000))  # noqa: SLF001
    r = await internal_client.post("/internal/v1/admin/backup")
    assert r.status_code == 200, r.text

    sets = backup_mod._scan_sets(db.data_dir / "backups")  # noqa: SLF001
    assert sets.get(fresh_label) == {"main", "work"}
    assert len(sets) == 7


async def test_orphan_plaintext_is_removed_even_when_backup_answers_409(internal_client, db) -> None:
    """Находка 1: уборка осиротевшей открытой копии не зависит от recipient/age."""
    run_dir = db.data_dir / "backups" / ".work" / "run-dead"
    run_dir.mkdir(parents=True)
    (run_dir / "main-20260101-000000.tmp").write_bytes(b"plaintext")
    (run_dir.parent / "main-20260101-000000.tmp").write_bytes(b"plaintext")  # раскладка первой редакции

    r = await internal_client.post("/internal/v1/admin/backup")
    assert r.status_code == 409, r.text
    assert list((db.data_dir / "backups" / ".work").iterdir()) == []


def test_cleanup_plaintext_on_core_start_is_wired() -> None:
    """Старт core обязан звать уборку ДО открытия БД — иначе копия ждёт следующего успешного бэкапа."""
    import inspect

    from hub.core import runner

    src = inspect.getsource(runner._serve_forever)  # noqa: SLF001
    assert src.index("cleanup_plaintext(data_dir)") < src.index("Db(data_dir")


async def test_existing_wide_dirs_are_tightened(
    internal_client, db, config_dir, tmp_path, monkeypatch
) -> None:
    """Находка 2: `mkdir(exist_ok=True)` прав существующего каталога не меняет."""
    backup_recipient.write_recipient(config_dir, "age1" + "x" * 58)
    _install_copy_age(tmp_path, monkeypatch)
    work_dir = db.data_dir / "backups" / ".work"
    work_dir.mkdir(parents=True)
    os.chmod(work_dir.parent, 0o755)  # noqa: S103 — намеренно широкие права, тест их и проверяет
    os.chmod(work_dir, 0o775)  # noqa: S103

    r = await internal_client.post("/internal/v1/admin/backup")
    assert r.status_code == 200, r.text
    assert (work_dir.parent.stat().st_mode & 0o777) == 0o700
    assert (work_dir.stat().st_mode & 0o777) == 0o700
    assert list(work_dir.iterdir()) == []


async def test_symlinked_work_dir_is_refused(internal_client, db, config_dir, tmp_path, monkeypatch) -> None:
    """Находка 2: подложенный симлинк `.work` увёл бы открытую копию за пределы data_dir."""
    backup_recipient.write_recipient(config_dir, "age1" + "x" * 58)
    _install_copy_age(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    backups_dir = db.data_dir / "backups"
    backups_dir.mkdir()

    real_cleanup = backup_mod.cleanup_plaintext

    # Симлинк появляется ПОСЛЕ входной уборки (та сама снимает симлинк) — моделируем подмену между шагами.
    def _cleanup_then_plant(data_dir):
        real_cleanup(data_dir)
        (backups_dir / ".work").symlink_to(outside)

    monkeypatch.setattr(backup_mod, "cleanup_plaintext", _cleanup_then_plant)

    r = await internal_client.post("/internal/v1/admin/backup")
    assert r.status_code == 500, r.text
    assert list(outside.iterdir()) == []
    assert list(backups_dir.glob("*.age")) == []


def test_work_is_copied_before_main(db) -> None:
    """Находка 5: main снимается ПОСЛЕ work — в копии main есть родители всех строк копии work."""
    assert [f.name for f in backup_mod._db_files(db)] == ["work", "main"]  # noqa: SLF001


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("age1abc023", "age1abc023"),
        ("  age1abc023  ", "age1abc023"),
        ("age1abc023 # мой ключ", None),
        ("age1abc023 мусор", None),
        ("AGE1ABC023", None),
        ("age1", None),
        ("-age1abc", None),
        ("age1abc-def", None),
        ("ssh-ed25519 AAAA", None),
    ],
)
def test_recipient_line_validation(tmp_path, line, expected) -> None:
    (tmp_path / backup_recipient.RECIPIENT_FILE_NAME).write_text(
        f"# комментарий\n\n{line}\n", encoding="utf-8"
    )
    assert backup_recipient.read_recipient(tmp_path) == expected


def test_write_recipient_rejects_garbage_and_keeps_old_file(tmp_path) -> None:
    backup_recipient.write_recipient(tmp_path, "age1good")
    with pytest.raises(ValueError, match="age1"):
        backup_recipient.write_recipient(tmp_path, "age1good --armor")
    assert backup_recipient.read_recipient(tmp_path) == "age1good"


# --- Тест 14 (кусок 1a фазы 1): бэкап ждёт активный backfill (раздел E спеки phase1-01-core-spec.md) --------


def _setup_fake_age_copy(tmp_path, config_dir, monkeypatch) -> None:
    backup_recipient.write_recipient(config_dir, "age1" + "x" * 58)
    fake_age = tmp_path / "age"
    _write_fake_age(
        fake_age,
        """#!/bin/sh
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -r) shift 2 ;;
    *) in="$1"; shift ;;
  esac
done
cp "$in" "$out"
""",
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")


async def _set_activity(internal_app, secret: str, *, backfill: bool) -> None:
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=internal_app)
    async with AsyncClient(transport=transport, base_url="http://hub-internal") as c:
        r = await c.post(
            "/internal/v1/account/activity",
            headers={"Authorization": f"Bearer {secret}"},
            json={"backfill": backfill},
        )
        assert r.status_code == 200, r.text


def _make_connector_secret(db, account_id: int) -> str:
    from hub.core import connector_auth

    secret = connector_auth.generate_connector_secret()
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.connector_secret (account_id, hash, created_ts) VALUES (?, ?, 0)",
            (account_id, connector_auth.hash_secret(secret)),
        )
    )
    return secret


async def test_1a_14_backup_waits_for_active_backfill_then_runs(
    internal_app, internal_client, db, clock, config_dir, tmp_path, monkeypatch
) -> None:
    _setup_fake_age_copy(tmp_path, config_dir, monkeypatch)
    account_id = insert_account(db, source="fake")
    secret = _make_connector_secret(db, account_id)
    await _set_activity(internal_app, secret, backfill=True)

    polls = {"n": 0}

    async def _fast_sleep(_seconds: float) -> None:
        polls["n"] += 1
        clock.advance_ms(10_000)
        if polls["n"] >= 2:
            await _set_activity(internal_app, secret, backfill=False)

    internal_app.state.backup_wait_sleep = _fast_sleep

    r = await internal_client.post("/internal/v1/admin/backup")
    assert r.status_code == 200, r.text
    assert polls["n"] >= 2

    with db.reader() as conn:
        detail = conn.execute(
            "SELECT detail_json FROM main.audit WHERE action = 'backup' AND result = 'ok'"
        ).fetchone()[0]
    waited_ms = json.loads(detail)["waited_ms"]
    assert waited_ms >= 20_000


async def test_1a_14_backup_after_backfill_ends_runs_immediately(
    internal_app, internal_client, db, config_dir, tmp_path, monkeypatch
) -> None:
    _setup_fake_age_copy(tmp_path, config_dir, monkeypatch)
    account_id = insert_account(db, source="fake")
    secret = _make_connector_secret(db, account_id)
    await _set_activity(internal_app, secret, backfill=True)
    await _set_activity(internal_app, secret, backfill=False)

    async def _fail_sleep(_seconds: float) -> None:
        raise AssertionError("бэкап не должен ждать — backfill уже снят")

    internal_app.state.backup_wait_sleep = _fail_sleep

    r = await internal_client.post("/internal/v1/admin/backup")
    assert r.status_code == 200, r.text

    with db.reader() as conn:
        detail = conn.execute(
            "SELECT detail_json FROM main.audit WHERE action = 'backup' AND result = 'ok'"
        ).fetchone()[0]
    assert json.loads(detail)["waited_ms"] == 0


async def test_1a_14_backup_gives_up_waiting_after_30min_and_runs_anyway(
    internal_app, internal_client, db, clock, config_dir, tmp_path, monkeypatch
) -> None:
    _setup_fake_age_copy(tmp_path, config_dir, monkeypatch)
    account_id = insert_account(db, source="fake")
    secret = _make_connector_secret(db, account_id)
    await _set_activity(internal_app, secret, backfill=True)  # backfill НИКОГДА не снимается

    async def _fast_sleep(_seconds: float) -> None:
        clock.advance_ms(60_000)  # часы прыгают минутами — 30 мин истекут быстро
        await _set_activity(internal_app, secret, backfill=True)  # коннектор продлевает TTL

    internal_app.state.backup_wait_sleep = _fast_sleep

    r = await internal_client.post("/internal/v1/admin/backup")
    assert r.status_code == 200, r.text

    with db.reader() as conn:
        detail = conn.execute(
            "SELECT detail_json FROM main.audit WHERE action = 'backup' AND result = 'ok'"
        ).fetchone()[0]
    waited_ms = json.loads(detail)["waited_ms"]
    assert waited_ms >= 30 * 60 * 1000
