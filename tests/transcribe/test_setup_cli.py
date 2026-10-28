"""`hub transcribe setup|status|run` и клиент ядра службы (Ф14-6, docs/architecture.md). `setup` — против НАСТОЯЩЕГО
ядра дочерним процессом (свободный порт, временные каталоги), как
`tests/agent/test_agent_cli.py`; проверка движка — подделкой `runner`."""

import json
import subprocess
import sys

import httpx
import pytest

from hub import cli as hub_cli
from hub.transcribe import files
from hub.transcribe import main as tmain
from hub.transcribe.core_api import CoreError, CoreUnauthorized, CoreUnavailable, TranscribeCore

from .conftest import api_get as _api
from .conftest import mode_of as _mode

_OK_CHECK = json.dumps(
    {"ok": True, "python": "3.12.3", "faster_whisper": "1.2.1", "model": "/m/turbo", "cublas": "/lib"}
)


def _runner(stdout: str = _OK_CHECK, code: int = 0, calls: list | None = None):
    def run(argv, **kwargs):
        if calls is not None:
            calls.append(argv)
        return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr="")

    return run


def test_setup_token_files_rotation_and_engine_check(real_core) -> None:
    data_dir, config_dir, port = real_core
    calls: list = []
    with hub_cli._admin_client() as admin:
        ok, text = tmain.run_setup(
            admin,
            config_dir=config_dir,
            data_dir=data_dir,
            engine_python="/opt/py/bin/python3",
            model_dirs=["/m/turbo"],
            runner=_runner(calls=calls),
        )
    assert ok, text
    token_file = files.token_path(config_dir)
    assert _mode(token_file.parent) == "0o700" and _mode(token_file) == "0o600"
    assert _mode(files.data_service_dir(data_dir)) == "0o700"  # П11: каталог юнита — сразу
    assert _mode(files.tmp_dir(data_dir)) == "0o700"
    first = files.read_secret(token_file)
    assert first and first not in text
    config = json.loads(files.config_path(config_dir).read_text())
    assert config["engine_python"] == "/opt/py/bin/python3" and config["model_dirs"] == ["/m/turbo"]
    assert calls == [["/opt/py/bin/python3", str(tmain.ENGINE_PATH), "--check", "--model-dir", "/m/turbo"]]
    assert "faster-whisper 1.2.1" in text and "hub-transcribe.service" in text
    # токен службы: лиз работает (пустая очередь — 204), чтение бесед — тоже (скоуп read), черновик — нет
    with httpx.Client(base_url=f"http://127.0.0.1:{port}/api/v1", trust_env=False) as c:
        r = c.post("/transcripts/lease", json={"wait_s": 0}, headers={"Authorization": f"Bearer {first}"})
        assert r.status_code == 204
    assert _api(port, first, "/projects").status_code == 200

    with hub_cli._admin_client() as admin:
        ok2, text2 = tmain.run_setup(admin, config_dir=config_dir, data_dir=data_dir, runner=_runner())
        tokens = admin.get("/admin/tokens").json()
    assert ok2
    second = files.read_secret(token_file)
    assert second != first
    assert _api(port, first, "/projects").status_code == 401  # прежний отозван
    assert "отозван" in text2
    live = [t for t in tokens if t["name"] == files.TOKEN_NAME and not t["revoked"]]
    assert len(live) == 1 and live[0]["kind"] == "background"
    assert sorted(live[0]["scopes"]) == ["read", "transcribe"]
    # флаги не заданы — прежние настройки сохраняются
    assert json.loads(files.config_path(config_dir).read_text())["engine_python"] == "/opt/py/bin/python3"


def test_setup_cli_fails_when_engine_not_ready(real_core, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        tmain, "check_engine", lambda config, runner=None: (False, "движок не готов: нет модели")
    )
    old = sys.argv
    sys.argv = ["hub", "transcribe", "setup"]
    try:
        with pytest.raises(SystemExit) as exc:
            hub_cli.main()
    finally:
        sys.argv = old
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "ВНИМАНИЕ: движок не готов: нет модели" in out


def test_check_engine_outcomes() -> None:
    config = {"engine_python": "/py", "model_dirs": ["/a", "/b"]}
    ok, text = tmain.check_engine(config, runner=_runner())
    assert ok and "cuBLAS есть" in text
    ok, text = tmain.check_engine(
        config, runner=_runner(json.dumps({"ok": False, "error": "faster_whisper не импортируется"}), 1)
    )
    assert not ok and "faster_whisper не импортируется" in text
    ok, text = tmain.check_engine(config, runner=_runner("мусор", 1))
    assert not ok and "не ответил" in text

    def missing(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    ok, text = tmain.check_engine(config, runner=missing)
    assert not ok and "не запускается" in text


def test_status_text_without_texts(tmp_path) -> None:
    data_dir = tmp_path / "data"
    out = tmain.status_text(data_dir, None)
    assert "ядро не ответило" in out and "ещё не запускалась" in out
    files.save_state(
        data_dir,
        {
            "engine_alive": True,
            "engine_pid": 4242,
            "counters": {"done": 3, "skip": 1},
            "errors": [{"ts": 1_700_000_000_000, "where": "media 9", "error": "нет звука (ffmpeg)"}],
            "last_ok_ts": 1_700_000_000_000,
            "updated_ts": 1_700_000_000_000,
        },
    )
    queue = {"configured": False, "queued": 2, "working": 1, "failed_24h": 0, "oldest_queued_ts": None}
    out = tmain.status_text(data_dir, queue)
    assert "ждут 2, в работе 1" in out and "hub transcribe setup" in out
    assert "жив (pid 4242)" in out and '"done": 3' in out and "media 9: нет звука (ffmpeg)" in out


def test_run_without_token_exits_78(tmp_path) -> None:
    code = tmain.run(config_dir=tmp_path / "c", data_dir=tmp_path / "d", base_url="http://127.0.0.1:9/api/v1")
    assert code == tmain.EXIT_NO_TOKEN == 78


# --- клиент ядра ---------------------------------------------------------------------------------------


def _core(tmp_path, handler) -> TranscribeCore:
    token = tmp_path / "token"
    token.write_text("t1\n")
    return TranscribeCore(
        token_file=token, base_url="http://core/api/v1", transport=httpx.MockTransport(handler)
    )


async def test_core_client_lease_and_errors(tmp_path) -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers["Authorization"], request.headers["X-Hub-Client"]))
        if request.url.path.endswith("/lease"):
            return httpx.Response(204) if len(seen) == 1 else httpx.Response(200, json={"media_id": 5})
        if request.url.path.endswith("/report"):
            return httpx.Response(409, json={"error": "stale_lease"})
        return httpx.Response(503)

    core = _core(tmp_path, handler)
    try:
        assert await core.lease(1) is None
        assert await core.lease(1) == {"media_id": 5}
        with pytest.raises(CoreError) as exc:
            await core.report(5, {"outcome": "done"})
        assert exc.value.status == 409
        with pytest.raises(CoreUnavailable):
            await core.extend(5, 1, 3.0)
    finally:
        await core.aclose()
    assert seen[0] == ("/api/v1/transcripts/lease", "Bearer t1", "transcribe:worker")


async def test_core_client_rereads_token_once_then_unauthorized(tmp_path) -> None:
    tokens = []

    def handler(request: httpx.Request) -> httpx.Response:
        tokens.append(request.headers["Authorization"])
        return (
            httpx.Response(200, json={})
            if request.headers["Authorization"] == "Bearer t2"
            else httpx.Response(401)
        )

    core = _core(tmp_path, handler)
    try:
        (tmp_path / "token").write_text("t2\n")  # повторный setup записал новый
        assert await core.lease(1) == {}
        (tmp_path / "token").write_text("t3\n")
        core._token = "t3"
        with pytest.raises(CoreUnauthorized):
            await core.lease(1)
    finally:
        await core.aclose()
    assert tokens[:2] == ["Bearer t1", "Bearer t2"]


async def test_core_client_download_writes_private_file_and_404(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/media/1"):
            return httpx.Response(200, content=b"OggS" + b"\0" * 100)
        return httpx.Response(404, json={"error": "not_found"})

    core = _core(tmp_path, handler)
    try:
        dest = tmp_path / "1.src"
        assert await core.download(1, dest) == 104
        assert oct(dest.stat().st_mode & 0o777) == "0o600"
        missing = tmp_path / "2.src"
        with pytest.raises(CoreError) as exc:
            await core.download(2, missing)
        assert exc.value.status == 404 and not missing.exists()
    finally:
        await core.aclose()
