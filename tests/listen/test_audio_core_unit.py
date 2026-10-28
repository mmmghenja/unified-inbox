"""Ф60-1, часть B: конфиг эхоподавления, `PulseBackend` на подменных `pactl`/`pipewire`/`ffmpeg`, клиент ядра,
юнит `hub-listen.service` и скрипты установки, `hub listen run` без токена."""

import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import httpx
import pytest

from hub.cli import _build_parser
from hub.listen import audio, files, service
from hub.listen.core_api import CoreUnauthorized, ListenCore

REPO = Path(__file__).resolve().parents[2]


def test_aec_config_names_targets_and_priorities():
    conf = audio.aec_config(audio.aec_node_prefix(4242, 3), "alsa_in.x", 'alsa_out"y')
    assert 'node.name = "hub_listen_aec_4242_3_source"' in conf
    assert 'node.name = "hub_listen_aec_4242_3_capture" target.object = "alsa_in.x"' in conf
    # П12: опора — сток по умолчанию; кавычки из имени не ломают конфиг.
    assert 'node.name = "hub_listen_aec_4242_3_sink" target.object = "alsa_outy"' in conf
    assert 'node.name = "hub_listen_aec_4242_3_playback" target.object = "alsa_outy"' in conf
    assert "monitor.mode = true" in conf and "aec/libspa-aec-webrtc" in conf
    assert conf.count("priority.session = 1") == 2
    assert "core.daemon" not in conf  # клиент общего PipeWire, своего сокета нет


def test_ffmpeg_argv_pipes_lpcm_16k_mono():
    argv = audio.ffmpeg_argv("hub_listen_aec_1_1_source")
    assert argv[:1] == ["ffmpeg"]
    assert argv[argv.index("-f") + 1] == "pulse"
    assert argv[argv.index("-i") + 1] == "hub_listen_aec_1_1_source"
    assert argv[argv.index("-ac") + 1] == "1" and argv[argv.index("-ar") + 1] == "16000"
    assert argv[-1] == "-" and "s16le" in argv


def _script(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


@pytest.fixture
def fake_bin(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    return bindir


async def test_pulse_backend_defaults_and_aec_ready(fake_bin, tmp_path, monkeypatch):
    monkeypatch.setattr(audio, "AEC_READY_S", 3.0)
    state = tmp_path / "aec_up"
    _script(
        fake_bin / "pactl",
        f"""case "$1" in
get-default-source) echo mic_x ;;
get-default-sink) echo spk_x ;;
list) echo "1\tmic_x\tPipeWire"; [ -f {state} ] && echo "2\thub_listen_aec_77_1_source\tPipeWire" ;;
esac
exit 0
""",
    )
    # «pipewire»: проверяет, что конфиг лежит во временном каталоге (0600), объявляет источник и живёт.
    _script(
        fake_bin / "pipewire",
        f"""[ "$1" = "-c" ] || exit 9
grep -q hub_listen_aec_77_1_source "$2" || exit 8
touch {state}
exec sleep 30
""",
    )
    backend = audio.PulseBackend(pid=77)
    assert await backend.defaults() == ("mic_x", "spk_x")
    aec = await backend.start_aec("mic_x", "spk_x")
    assert aec is not None and aec.source == "hub_listen_aec_77_1_source"
    conf = aec.tmpdir / "aec.conf"
    assert stat.S_IMODE(conf.stat().st_mode) == 0o600 and str(aec.tmpdir).startswith(str(tmp_path / "tmp"))
    await aec.stop()
    assert aec.child.proc.returncode is not None and not aec.tmpdir.exists()


async def test_pulse_backend_aec_dead_returns_none_and_cleans_up(fake_bin, tmp_path, monkeypatch):
    monkeypatch.setattr(audio, "AEC_READY_S", 2.0)
    _script(fake_bin / "pactl", 'echo "1\tmic_x\tPipeWire"\n')
    _script(fake_bin / "pipewire", "echo 'нет модуля' >&2; exit 1\n")
    backend = audio.PulseBackend(pid=78)
    assert await backend.start_aec("mic_x", "spk_x") is None
    assert list((tmp_path / "tmp").iterdir()) == []


async def test_pulse_backend_aec_missing_binary_returns_none(fake_bin, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(fake_bin))  # нет ни pipewire, ни pactl
    backend = audio.PulseBackend(pid=79)
    assert await backend.start_aec("mic_x", "spk_x") is None
    with pytest.raises(audio.AudioError):
        await backend.defaults()


async def test_pulse_backend_capture_reads_pipe_and_eof(fake_bin):
    _script(fake_bin / "ffmpeg", 'printf "abcd"\n')
    capture = await audio.PulseBackend().start_capture("spk_x.monitor")
    data = b""
    while chunk := await capture.read():
        data += chunk
    assert data == b"abcd"
    await capture.stop()


async def test_listen_core_chunk_current_warn_and_auth(tmp_path):
    cfg = tmp_path / "cfg"
    files.private_dir(files.config_service_dir(cfg))
    files.write_secret(files.token_path(cfg), "tok_one")
    seen: list[httpx.Request] = []
    status = {"chunk": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/listen/current"):
            return httpx.Response(
                200, json={"interview": {"id": 3, "status": "active", "them_lang": "ru-RU"}}
            )
        if request.url.path.endswith("/chunk"):
            return httpx.Response(status["chunk"], json={"detail": "interview_not_active"})
        if request.url.path.endswith("/warn"):
            return httpx.Response(200, json={"warn": None})
        return httpx.Response(404)

    core = ListenCore(
        token_file=files.token_path(cfg),
        base_url="http://core/api/v1",
        transport=httpx.MockTransport(handler),
    )
    assert (await core.current())["id"] == 3
    reply = await core.chunk(
        3, who="me", start_ms=1_700_000_000_000, end_ms=1_700_000_001_000, pcm=b"\1\0" * 16000
    )
    req = seen[-1]
    assert reply.status == 200
    assert req.url.path == "/api/v1/listen/3/chunk"
    assert dict(req.url.params) == {"who": "me", "start_ms": "1700000000000", "end_ms": "1700000001000"}
    assert req.headers["content-length"] == "32000" and req.headers["authorization"] == "Bearer tok_one"
    assert req.headers["x-hub-client"] == "listen:recorder"
    status["chunk"] = 409
    assert (
        await core.chunk(3, who="them", start_ms=1, end_ms=2, pcm=b"\0\0")
    ).detail == "interview_not_active"
    assert (await core.warn(3, "без эхоподавления")).status == 200
    assert json.loads(seen[-1].content) == {"text": "без эхоподавления"}
    await core.aclose()

    def deny(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "forbidden"})

    core = ListenCore(
        token_file=files.token_path(cfg), base_url="http://core", transport=httpx.MockTransport(deny)
    )
    with pytest.raises(CoreUnauthorized):
        await core.current()
    await core.aclose()


def test_run_without_token_exits_78(tmp_path):
    assert service.run(config_dir=tmp_path / "cfg", base_url="http://127.0.0.1:9/api/v1") == 78
    args = _build_parser().parse_args(["listen", "run", "--url", "http://127.0.0.1:8800/api/v1"])
    assert args.listen_command == "run" and args.url == "http://127.0.0.1:8800/api/v1"


def test_unit_is_manual_sandboxed_and_restarted_by_deploy_only_if_active():
    unit = (REPO / "deploy" / "systemd" / "hub-listen.service").read_text(encoding="utf-8")
    assert "\nExecStart=%h/projects/hub-prod/.venv/bin/hub listen run\n" in unit
    assert "\nRestartPreventExitStatus=78\n" in unit
    assert "\nReadWritePaths=%h/.local/share/hub/listen\n" in unit
    assert "\nProtectSystem=strict\n" in unit and "\nProtectHome=read-only\n" in unit
    assert "\nPrivateTmp=yes\n" in unit
    assert "\nMemoryMax=512M\n" in unit
    script = (REPO / "scripts" / "hub-units-install.sh").read_text(encoding="utf-8")
    manual = script[script.index("MANUAL_UNITS=(") : script.index(")", script.index("MANUAL_UNITS=("))]
    assert "hub-listen.service" in manual
    assert '"$HOME/.local/share/hub/listen"' in script
    for line in script.splitlines():
        if line.startswith("systemctl"):
            assert "hub-listen" not in line
    deploy = (REPO / "scripts" / "hub-deploy.sh").read_text(encoding="utf-8")
    assert "is-active --quiet hub-listen.service" in deploy


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze не установлен")
@pytest.mark.skipif(
    not (Path.home() / "projects/hub-prod/.venv/bin/hub").exists(),
    reason="нет боевого дерева ~/projects/hub-prod — verify требует исполняемый ExecStart",
)
def test_unit_passes_systemd_analyze_verify():
    r = subprocess.run(
        ["systemd-analyze", "--user", "verify", str(REPO / "deploy" / "systemd" / "hub-listen.service")],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ},
    )
    assert r.returncode == 0, r.stdout + r.stderr
