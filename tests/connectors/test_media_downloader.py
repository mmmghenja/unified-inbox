"""Ф8-5 (5b, П3/П4/П15): общий загрузчик медиа коннектора против фейкового ядра — временный файл, PUT с
`Content-Length`, потолок `max_bytes`, отчёты, пауза после FloodWait, 401 ядра."""

from __future__ import annotations

import asyncio
import os
import stat

import pytest

from hub.connectors.base import AuthRequired
from hub.connectors.media import MediaDownloader, MediaResult

from .media_fakes import MIB, FakeMediaCore, media_job


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def mono(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s
        await asyncio.sleep(0)


def make(core: FakeMediaCore, fetch, tmp_path, clock: Clock | None = None, **kw):
    clock = clock or Clock()
    state = {"auth": 0, "unauth": 0}

    async def on_auth():
        state["auth"] += 1

    def on_unauth():
        state["unauth"] += 1

    d = MediaDownloader(
        core.client(),
        fetch,
        tmp_dir=tmp_path / "media" / ".tmp-conn" / "7",
        mono=clock.mono,
        sleep=clock.sleep,
        is_blocked=kw.get("is_blocked", lambda: False),
        on_auth_required=on_auth,
        on_unauthorized=on_unauth,
    )
    return d, state


async def test_ready_puts_file_with_content_length_and_removes_tmp(tmp_path):
    core = FakeMediaCore()
    core.jobs = [media_job(5, lease_gen=3)]
    seen: dict = {}

    async def fetch(job, sink):
        tmp_files = list((tmp_path / "media" / ".tmp-conn" / "7").iterdir())
        seen["mode"] = stat.S_IMODE(os.stat(tmp_files[0]).st_mode)
        seen["dir_mode"] = stat.S_IMODE(os.stat(tmp_files[0].parent).st_mode)
        sink.write(b"abc")
        sink.write(b"defg")
        return MediaResult("ready")

    d, _ = make(core, fetch, tmp_path)
    assert await d.step() == 1
    assert core.lease_calls == [{"limit": 1}]
    assert len(core.puts) == 1
    put = core.puts[0]
    assert put["media_id"] == 5 and put["lease_gen"] == 3
    assert put["body"] == b"abcdefg"
    assert put["content_length"] == "7"
    assert put["transfer_encoding"] is None
    assert core.reports == []
    assert seen == {"mode": 0o600, "dir_mode": 0o700}
    assert list((tmp_path / "media" / ".tmp-conn" / "7").iterdir()) == []


async def test_known_size_over_max_bytes_reports_too_big_without_fetch(tmp_path):
    core = FakeMediaCore()
    core.jobs = [media_job(1, size=25 * MIB, max_bytes=20 * MIB)]
    calls = []

    async def fetch(job, sink):
        calls.append(job)
        return MediaResult("ready")

    d, _ = make(core, fetch, tmp_path)
    await d.step()
    assert calls == []
    assert core.puts == []
    assert core.reports[0]["outcome"] == "too_big"
    assert core.reports[0]["size"] == 25 * MIB


async def test_unknown_size_stream_over_max_bytes_is_cut_too_big(tmp_path):
    core = FakeMediaCore()
    core.jobs = [media_job(1, size=None, max_bytes=10)]

    async def fetch(job, sink):
        sink.write(b"123456")
        sink.write(b"789012")  # 12 > 10
        return MediaResult("ready")

    d, _ = make(core, fetch, tmp_path)
    await d.step()
    assert core.puts == []
    assert [r["outcome"] for r in core.reports] == ["too_big"]
    assert list((tmp_path / "media" / ".tmp-conn" / "7").iterdir()) == []


async def test_gone_and_temporary_reported_with_lease_gen(tmp_path):
    core = FakeMediaCore()
    core.jobs = [media_job(1, lease_gen=4), media_job(2, lease_gen=9)]
    results = [MediaResult("gone", error="no_message"), MediaResult("temporary", error="OSError")]

    async def fetch(job, sink):
        return results.pop(0)

    d, _ = make(core, fetch, tmp_path)
    await d.step()
    await d.step()
    assert [(r["media_id"], r["lease_gen"], r["outcome"], r["error"]) for r in core.reports] == [
        (1, 4, "gone", "no_message"),
        (2, 9, "temporary", "OSError"),
    ]
    assert "retry_after_s" not in core.reports[0]


async def test_exception_in_fetch_is_temporary_and_loop_survives(tmp_path):
    core = FakeMediaCore()
    core.jobs = [media_job(1)]

    async def fetch(job, sink):
        raise RuntimeError("boom")

    d, _ = make(core, fetch, tmp_path)
    await d.step()
    assert core.reports[0]["outcome"] == "temporary"
    assert core.reports[0]["error"] == "RuntimeError"


async def test_flood_pauses_downloader(tmp_path):
    core = FakeMediaCore()
    core.jobs = [media_job(1)]
    clock = Clock()

    async def fetch(job, sink):
        return MediaResult("temporary", error="FloodWait", retry_after_s=120.0)

    d, _ = make(core, fetch, tmp_path, clock)
    await d.step()
    assert core.reports[0]["retry_after_s"] == 120.0
    assert d.paused_until == clock.t + 120.0

    # Цикл сначала выжидает паузу и только потом снова спрашивает ядро.
    task = asyncio.ensure_future(d.run())
    for _ in range(50):
        await asyncio.sleep(0)
        if clock.sleeps:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert clock.sleeps[0] == 120.0


async def test_put_409_is_logged_and_next_job_goes(tmp_path):
    core = FakeMediaCore()
    core.put_status = 409
    core.jobs = [media_job(1), media_job(2)]

    async def fetch(job, sink):
        sink.write(b"x")
        return MediaResult("ready")

    d, _ = make(core, fetch, tmp_path)
    await d.step()
    await d.step()
    assert [p["media_id"] for p in core.puts] == [1, 2]
    assert list((tmp_path / "media" / ".tmp-conn" / "7").iterdir()) == []


async def test_put_401_stops_loop_with_unauthorized(tmp_path):
    core = FakeMediaCore()
    core.put_status = 401
    core.jobs = [media_job(1)]

    async def fetch(job, sink):
        sink.write(b"x")
        return MediaResult("ready")

    d, state = make(core, fetch, tmp_path)
    await asyncio.wait_for(d.run(), 5)
    assert state["unauth"] == 1


async def test_auth_required_from_fetch_calls_hook_without_report(tmp_path):
    core = FakeMediaCore()
    core.jobs = [media_job(1)]

    async def fetch(job, sink):
        raise AuthRequired("lost")

    d, state = make(core, fetch, tmp_path)
    await d.step()
    assert state["auth"] == 1
    assert core.reports == [] and core.puts == []


async def test_blocked_takes_no_jobs(tmp_path):
    core = FakeMediaCore()
    core.jobs = [media_job(1)]
    clock = Clock()

    async def fetch(job, sink):
        raise AssertionError("не должно вызываться")

    d, _ = make(core, fetch, tmp_path, clock, is_blocked=lambda: True)
    task = asyncio.ensure_future(d.run())
    for _ in range(20):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert core.lease_calls == []


# --- Ф32-2 п.5а + П4: чистка осиротевших `*.part` на старте коннектора --------------------------------------


def test_f322_cleanup_parts_only_own_dir_no_recursion_no_links(tmp_path):
    from hub.connectors.media import cleanup_parts

    root = tmp_path / "media" / ".tmp-conn"
    own = root / "7"
    other = root / "8"
    sub = own / "sub"
    for d in (own, other, sub):
        d.mkdir(parents=True)
    (own / "5.3.part").write_bytes(b"x")
    (own / "outbox-abc.part").write_bytes(b"y")
    (own / "keep.bin").write_bytes(b"z")
    (sub / "9.1.part").write_bytes(b"deep")
    (other / "5.3.part").write_bytes(b"foreign")
    outside = tmp_path / "outside.part"
    outside.write_bytes(b"target")
    (own / "link.part").symlink_to(outside)
    (own / "dirlink.part").symlink_to(other, target_is_directory=True)
    (own / "folder.part").mkdir()

    assert cleanup_parts(own) == 2
    left = sorted(p.name for p in own.iterdir())
    assert left == ["dirlink.part", "folder.part", "keep.bin", "link.part", "sub"]
    assert (sub / "9.1.part").exists()  # без рекурсии
    assert (other / "5.3.part").read_bytes() == b"foreign"  # чужой каталог аккаунта
    assert outside.read_bytes() == b"target"  # ссылка не разыменована


def test_f322_cleanup_parts_missing_or_linked_dir_is_noop(tmp_path):
    from hub.connectors.media import cleanup_parts

    assert cleanup_parts(tmp_path / "nope") == 0
    real = tmp_path / "real"
    real.mkdir()
    (real / "1.1.part").write_bytes(b"x")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert cleanup_parts(link) == 0
    assert (real / "1.1.part").exists()
