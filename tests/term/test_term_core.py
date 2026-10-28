"""Ф56-5, кусок 5а: мост к tmux на настоящем tmux (раздел 4 «5а» + поправки К5–К8, К11).

Создание, ввод, вывод с кириллицей и цветом, снимок с режимами, отставший подписчик -> снимок, запуск со
скобками вставки и без, `exit` -> `closed`, переживает перезапуск моста, порядок ввода, окружение оболочки."""

import asyncio
import base64
import json
from pathlib import Path

import pytest

from hub.term import stream as stream_mod
from hub.term import text as text_mod
from hub.term.bridge import TermGone, unescape
from hub.term.config import TermConfig
from hub.term.hub import TermBusy, TermHub, TermLimit
from hub.term.ring import Ring
from hub.term.tmux import OWNER_UI, owner_of_token

from .conftest import make_cfg, ring_text, wait_for, wait_out


def _counter():
    state = {"n": 0}

    async def alloc() -> int:
        state["n"] += 1
        return state["n"]

    return alloc


async def _new(hub: TermHub, alloc, owner=OWNER_UI, cols=80, rows=24):
    info = await hub.create(owner=owner, alloc_id=alloc, cols=cols, rows=rows)
    bridge = await hub.bridge(info.id)

    async def _prompt() -> bool:
        return (await bridge.screen(5))["text"].rstrip().endswith("$")

    await wait_for(_prompt)  # приглашение могло прийти раньше моста — смотрим экран, не кольцо
    return info, bridge


def _frames(raw: list[str]) -> list[tuple[str, dict, str | None]]:
    result = []
    for frame in raw:
        if frame.startswith(":"):
            continue
        fields = dict(line.split(": ", 1) for line in frame.strip().split("\n"))
        result.append((fields["event"], json.loads(fields["data"]), fields.get("id")))
    return result


class _Reader:
    """Генератор потока в фоне: кадры копятся в список."""

    def __init__(self, gen) -> None:
        self.gen = gen
        self.raw: list[str] = []
        self.task = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        async for frame in self.gen:
            self.raw.append(frame)

    @property
    def frames(self):
        return _frames(self.raw)

    def data_of(self, term_id: int) -> bytes:
        out = b""
        for event, data, _ in self.frames:
            if data.get("term_id") != term_id:
                continue
            if event == "snapshot":
                out = base64.b64decode(data["data_b64"])
            elif event == "out":
                out += base64.b64decode(data["data_b64"])
        return out

    async def stop(self) -> None:
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError, StopAsyncIteration:
            pass


def test_unescape_and_ring():
    assert unescape(b"a\\033[31mb\\134c\\015\\012") == b"a\x1b[31mb\\c\r\n"
    ring = Ring(8)
    ring.append(b"0123456789")
    assert (ring.start, ring.end) == (2, 10)
    assert ring.read(4, 3) == b"456" and not ring.has(1) and ring.has(10) and not ring.has(11)
    with pytest.raises(ValueError):
        ring.read(1, 3)


def test_config_from_env_and_socket_limit(tmp_path):
    cfg = TermConfig.from_env(tmp_path, env={})
    assert (cfg.unit, cfg.socket, cfg.conf, cfg.use_systemd) == (
        "hub-term",
        tmp_path / "term" / "tmux.sock",
        tmp_path / "term" / "tmux.conf",
        True,
    )
    cfg = TermConfig.from_env(
        tmp_path,
        env={"HUB_TERM_UNIT": "hub-term-stand", "HUB_TERM_SOCKET": "/tmp/x.sock", "HUB_TERM_SYSTEMD": "0"},
    )
    assert (cfg.unit, str(cfg.socket), cfg.use_systemd) == ("hub-term-stand", "/tmp/x.sock", False)
    assert "длиннее" in TermConfig(socket=tmp_path / ("x" * 120), conf=tmp_path / "c").problem()
    bad_unit = TermConfig(socket=tmp_path / "s", conf=tmp_path / "c", unit="bad unit;rm")
    assert "недопустимо" in bad_unit.problem()
    assert TermConfig(socket=Path("/tmp/s"), conf=tmp_path / "c").problem() is None


def test_run_text_check():
    assert text_mod.check("ls -la\n\techo 'привет'") is None
    for bad in ("a\x1bb", "a\rb", "a\x7fb", "a\x85b", "a\u202eb", "a\u2066b", "a\x00", "   ", ""):
        assert text_mod.check(bad) is not None, repr(bad)
    assert text_mod.check("x" * (text_mod.TEXT_MAX + 1)) == "too_long"
    assert text_mod.prepare("echo 1\n\n") == b"echo 1"


async def test_io_cyrillic_color_snapshot_and_stream(term_hub):
    alloc = _counter()
    info, bridge = await _new(term_hub, alloc)
    assert info.id == 1 and info.owner == OWNER_UI and (info.cols, info.rows) == (80, 24)
    assert (term_hub.server.cfg.socket.parent.stat().st_mode & 0o777) == 0o700

    await bridge.send_bytes("echo привет; printf '\\033[31mred\\033[0m\\n'\r".encode())
    text = await wait_out(bridge, "\x1b[31mred")
    assert "привет\r\n" in text

    snap = await bridge.snapshot()
    assert snap["epoch"] == bridge.epoch and snap["pos"] <= bridge.ring.end
    assert (snap["cols"], snap["rows"]) == (80, 24) and snap["alt_screen"] is False
    assert "привет".encode() in snap["data"] and b"\x1b[31mred" in snap["data"]
    assert set(snap["modes"]) >= {
        "cursor_flag",
        "keypad_flag",
        "mouse_sgr_flag",
        "wrap_flag",
        "scroll_region",
    }
    assert snap["modes"]["scroll_region"] == [0, 23]
    assert snap["data"].count(b"\r\n") >= 23  # видимый экран целиком

    # Поток: новый подписчик получает снимок, затем живой вывод кадрами `out`.
    reader = _Reader(stream_mod.stream(term_hub, positions={}, heartbeat_s=30))
    await wait_for(lambda: any(e == "snapshot" for e, _, _ in reader.frames))
    event, data, frame_id = reader.frames[0]
    assert event == "snapshot" and data["term_id"] == 1 and data["pos"].startswith(bridge.epoch + ":")
    assert frame_id == f"1@{data['pos']}"
    await bridge.send_bytes(b"echo zz$((6*7))\r")
    await wait_for(lambda: b"zz42" in reader.data_of(1))
    outs = [d for e, d, _ in reader.frames if e == "out"]
    assert outs and int(outs[-1]["pos"].split(":")[1]) == bridge.ring.end
    await reader.stop()
    assert term_hub.subscribers == 0


async def test_resume_from_pos_and_lagging_subscriber_gets_snapshot(term_root):
    hub = TermHub(make_cfg(term_root, ring_bytes=4096))
    try:
        info, bridge = await _new(hub, _counter())
        await bridge.send_bytes(b"echo first\r")
        await wait_out(bridge, "first\r\n")
        pos = (bridge.epoch, bridge.ring.end)
        # Чужая эпоха (прежний мост) при позиции, которая есть в нынешнем кольце, — всё равно снимок (К7).
        assert bridge.ring.has(1)
        reader = _Reader(stream_mod.stream(hub, positions={info.id: ("deadbeef", 1)}, heartbeat_s=30))
        await wait_for(lambda: reader.frames)
        assert reader.frames[0][0] == "snapshot"
        await reader.stop()
        # Своя позиция внутри кольца — без снимка, сразу вывод с неё.
        reader = _Reader(stream_mod.stream(hub, positions={info.id: pos}, heartbeat_s=30))
        await bridge.send_bytes(b"echo again\r")
        await wait_for(lambda: any(e == "out" for e, _, _ in reader.frames))
        assert not any(e == "snapshot" for e, _, _ in reader.frames)
        await reader.stop()

        # Отстал за пределы кольца (вывод > 4 КиБ) — снимок.
        await bridge.send_bytes(b"for i in $(seq 1 1000); do echo line-$i; done\r")
        await wait_out(bridge, "line-1000")
        assert not bridge.ring.has(pos[1])
        reader = _Reader(stream_mod.stream(hub, positions={info.id: pos}, heartbeat_s=30))
        await wait_for(lambda: reader.frames)
        assert reader.frames[0][0] == "snapshot" and b"line-1000" in reader.data_of(info.id)
        await reader.stop()

    finally:
        await hub.aclose()
        await hub.server.kill_server()


async def test_run_bracketed_paste_decided_by_tmux(term_hub):
    _, bridge = await _new(term_hub, _counter())
    await term_hub.run(1, "echo run-$((40+2))", force=False)
    await wait_out(bridge, "run-42")

    # Программа включила скобки вставки — tmux оборачивает (`paste-buffer -p`).
    await bridge.send_bytes(b"printf '\\033[?2004h'; cat -v\r")
    await asyncio.sleep(0.3)
    with pytest.raises(TermBusy):  # в панели `cat`, не оболочка
        await term_hub.run(1, "abc", force=False)
    await term_hub.run(1, "abc", force=True)
    await wait_out(bridge, "^[[200~abc^[[201~")
    await bridge.send_bytes(b"\x04")
    await wait_for(lambda: ring_text(bridge).rstrip().endswith("$"))

    # Не включила — как есть, без скобок.
    start = bridge.ring.end
    await bridge.send_bytes(b"printf '\\033[?2004l'; cat -v\r")
    await asyncio.sleep(0.3)
    await term_hub.run(1, "xyz", force=True)
    await wait_for(lambda: "xyz" in bridge.ring.read(start, 1 << 20).decode(errors="replace"))
    await asyncio.sleep(0.2)
    assert "200~" not in bridge.ring.read(start, 1 << 20).decode(errors="replace")
    await bridge.send_bytes(b"\x04")


async def test_busy_on_alt_screen(term_hub):
    _, bridge = await _new(term_hub, _counter())
    await bridge.send_bytes(b"printf '\\033[?1049h'\r")

    async def _alt() -> bool:
        return (await bridge.state())[0]

    await wait_for(_alt)
    alt, command = await bridge.state()
    assert alt is True and command == "bash"
    with pytest.raises(TermBusy):
        await term_hub.run(1, "echo no", force=False)
    snap = await bridge.snapshot()
    assert snap["alt_screen"] is True


async def test_exit_closes_terminal_and_stream_says_closed(term_hub):
    info, bridge = await _new(term_hub, _counter())
    reader = _Reader(stream_mod.stream(term_hub, positions={}, heartbeat_s=30))
    await wait_for(lambda: reader.frames)
    await bridge.send_bytes(b"exit\r")
    await wait_for(lambda: any(e == "closed" and d == {"term_id": info.id} for e, d, _ in reader.frames))
    assert await term_hub.list() == [] and info.id not in term_hub.live_ids()
    with pytest.raises(TermGone):
        await term_hub.bridge(info.id)
    await reader.stop()


async def test_survives_bridge_restart(term_root):
    """Новый объект ядра (перезапуск) находит живую сессию; мост — новая эпоха, снимок с прежним экраном."""
    cfg = make_cfg(term_root)
    first = TermHub(cfg)
    try:
        info, bridge = await _new(first, _counter())
        await bridge.send_bytes(b"echo before-restart\r")
        await wait_out(bridge, "before-restart\r\n")
        old_epoch = bridge.epoch
        await first.aclose()  # ядро остановлено: клиенты управления закрыты, сервер tmux жив

        second = TermHub(cfg)
        try:
            infos = await second.list()
            assert [i.id for i in infos] == [info.id]
            reader = _Reader(stream_mod.stream(second, positions={info.id: (old_epoch, 10)}, heartbeat_s=30))
            await wait_for(lambda: reader.frames)
            event, data, _ = reader.frames[0]
            assert event == "snapshot" and not data["pos"].startswith(old_epoch)
            assert b"before-restart" in base64.b64decode(data["data_b64"])
            await reader.stop()
        finally:
            await second.aclose()
    finally:
        await first.server.kill_server()


async def test_limits_per_owner_and_growing_ids(term_hub):
    alloc = _counter()
    for _ in range(4):
        await term_hub.create(owner=OWNER_UI, alloc_id=alloc, cols=80, rows=24)
    with pytest.raises(TermLimit):
        await term_hub.create(owner=OWNER_UI, alloc_id=alloc, cols=80, rows=24)
    agent = owner_of_token(7)
    a1 = await term_hub.create(owner=agent, alloc_id=alloc, cols=80, rows=24)
    await term_hub.create(owner=agent, alloc_id=alloc, cols=80, rows=24)
    with pytest.raises(TermLimit):
        await term_hub.create(owner=agent, alloc_id=alloc, cols=80, rows=24)
    # Конкурентное создание под блокировкой: лишнего не бывает (выдача номера медленная — без блокировки
    # все трое увидели бы одно свободное место).
    await term_hub.kill(a1.id)

    async def slow_alloc() -> int:
        await asyncio.sleep(0.2)
        return await alloc()

    results = await asyncio.gather(
        *(term_hub.create(owner=agent, alloc_id=slow_alloc, cols=80, rows=24) for _ in range(3)),
        return_exceptions=True,
    )
    assert sum(1 for r in results if isinstance(r, TermLimit)) == 2
    owners = [i.owner for i in await term_hub.list()]
    assert owners.count(OWNER_UI) == 4 and owners.count(agent) == 2
    # Номер растёт и не переиспользуется (закрытый a1 не вернулся).
    assert sorted(i.id for i in await term_hub.list()) == [1, 2, 3, 4, 6, 7]


async def test_input_order_kept(term_hub):
    """К11: много вводов подряд без ожидания — в оболочку в том же порядке."""
    _, bridge = await _new(term_hub, _counter())
    await bridge.send_bytes(b"cat > /dev/null\r")
    await asyncio.sleep(0.2)
    start = bridge.ring.end
    await asyncio.gather(*(bridge.send_bytes(f"<{i:03d}>".encode()) for i in range(150)))
    expected = "".join(f"<{i:03d}>" for i in range(150))
    await wait_for(lambda: expected in bridge.ring.read(start, 1 << 20).decode(errors="replace"))
    await bridge.send_bytes(b"\x03")


async def test_resize_size_frame_and_screen(term_hub):
    info, bridge = await _new(term_hub, _counter())
    reader = _Reader(stream_mod.stream(term_hub, positions={}, heartbeat_s=30))
    await wait_for(lambda: reader.frames)
    await bridge.resize(100, 30)
    await wait_for(
        lambda: any(e == "size" and (d["cols"], d["rows"]) == (100, 30) for e, d, _ in reader.frames)
    )
    assert [(i.cols, i.rows) for i in await term_hub.list()] == [(100, 30)]
    await reader.stop()

    await bridge.send_bytes(b"echo screen-$((1+1)); sleep 30\r")
    await wait_out(bridge, "screen-2")
    await asyncio.sleep(0.2)
    screen = await bridge.screen(5)
    assert screen["command"] == "sleep" and screen["alt_screen"] is False
    assert screen["text"].splitlines()[-1] == "screen-2" and len(screen["text"].splitlines()) <= 5
    assert (screen["cols"], screen["rows"]) == (100, 30)
    with pytest.raises(TermBusy):
        await term_hub.run(info.id, "echo x", force=False)
    await bridge.send_bytes(b"\x03")


async def test_shell_env_has_no_core_vars(term_root, monkeypatch):
    """Переменных ядра (`HUB_*`) в окружении оболочки нет."""
    monkeypatch.setenv("HUB_SECRET_TEST", "leak")
    hub = TermHub(make_cfg(term_root))
    try:
        _, bridge = await _new(hub, _counter())
        await bridge.send_bytes(b"env | grep -c ^HUB_ ; echo end-$((2+3))\r")
        text = await wait_out(bridge, "end-5")
        tail = text.split("end-$((2+3))")[1]
        assert "0\r\nend-5" in tail and "leak" not in tail
    finally:
        await hub.aclose()
        await hub.server.kill_server()
