"""Ревью Ф60-1, находка 2: отмена записи во время подъёма эхоподавления гасит дочерний `pipewire -c` и чистит
временный каталог (иначе он держал бы микрофон до перезапуска службы). Подменные `pipewire` и `pactl`."""

import asyncio
import tempfile

import pytest

from hub.listen import audio


def _script(path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


async def test_cancel_while_aec_is_starting_stops_pipewire_and_cleans_tmpdir(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _script(bindir / "pipewire", "exec sleep 30\n")
    _script(bindir / "pactl", 'echo "1\tmic_x\tPipeWire"\n')  # источник эхоподавления не появляется
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    monkeypatch.setattr(audio, "AEC_READY_S", 20.0)

    backend = audio.PulseBackend(pid=91)
    spawned: list = []
    original = backend._spawn  # noqa: SLF001

    async def spy(argv, label, *, stdout):
        child = await original(argv, label, stdout=stdout)
        spawned.append(child)
        return child

    monkeypatch.setattr(backend, "_spawn", spy)
    task = asyncio.create_task(backend.start_aec("mic_x", "spk_x"))
    for _ in range(300):
        await asyncio.sleep(0.01)
        if spawned:
            break
    assert spawned, "pipewire не запустился"
    await asyncio.sleep(0.5)  # идёт ожидание источника
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    proc = spawned[0].proc
    try:
        try:
            await asyncio.wait_for(proc.wait(), 3.0)
        except TimeoutError:
            pytest.fail("pipewire эхоподавления остался жить после отмены записи")
        assert list(tmp.iterdir()) == [], "временный каталог с конфигом эхоподавления не удалён"
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
