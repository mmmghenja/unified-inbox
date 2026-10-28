"""Ф74-2 (docs/architecture.md; план 6.2 В2): `hub sub wait|leave --ext-session
S --ext-dir D [--ext-pid P]` — ожидание, которое держит хук хаба внешнего Codex/Cursor. Как у OpenCode: без
срока (простой не будит модель), тексты без «перезапусти». Ядро — подменный транспорт; сессия — подменная
`find_ext`."""

import io
import json
import sys

import httpx
import pytest

from hub import cli as hub_cli
from hub import session_registry as reg
from hub import sub_wait
from hub.mcp_server import profile as profile_mod

URL = "http://127.0.0.1:8787/api/v1"
COMMAND = "/opt/hub/.venv/bin/hub sub wait 17 --profile all"
CONV = "019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5b"
S_CX = reg.SessionInfo(100, 5000, f"codex:{CONV}", "/work/iip", None, None, "codex")


class Core:
    def __init__(self, responses):
        self.responses = list(responses)
        self.seen: list[tuple[str, str, dict, dict | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.seen.append((request.method, request.url.path, dict(request.url.params), body))
        status, payload = self.responses.pop(0)
        return httpx.Response(status, json=payload)


def _profile(tmp_path):
    config_dir = tmp_path / "config"
    profile_mod.write_profile(config_dir, "all", token="hubt_good", meta={"token_id": 1})
    return config_dir


class Clock:
    """Часы, которые уходят на сутки за каждый запрос: у Claude Code ожидание вышло бы по простою."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 86_400.0
        return self.t


def _run(tmp_path, core, *, ext_pid=100, session=S_CX):
    calls = []

    def find_ext(start, sid, ext_dir, *, ext_pid=None):
        calls.append((start, sid, ext_dir, ext_pid))
        if isinstance(session, BaseException):
            raise session
        return session

    out, err = io.StringIO(), io.StringIO()
    code = sub_wait.run_wait(
        17,
        profile="all",
        url=URL,
        config_dir=_profile(tmp_path),
        command=COMMAND,
        transport=httpx.MockTransport(core.handler),
        find=lambda _pid: pytest.fail("реестр Claude Code для внешнего Codex не читается"),
        getppid=lambda: 4242,
        sleep=lambda _s: None,
        out=out,
        err=err,
        clock=Clock(),
        ext_session=CONV,
        ext_dir="/work/iip",
        ext_pid=ext_pid,
        find_ext=find_ext,
    )
    return code, out.getvalue(), calls


def test_no_idle_deadline_and_neutral_signal_text(tmp_path):
    card = {"item_id": 845, "channel_id": 152, "account_id": 5, "label": "urgent", "msg_count": 2}
    core = Core(
        [
            (200, {"sub_id": 17, "project_id": 3, "signals": []}),
            (200, {"sub_id": 17, "project_id": 3, "signals": []}),
            (200, {"sub_id": 17, "project_id": 3, "signals": [card]}),
        ]
    )
    code, out, calls = _run(tmp_path, core)
    # Часы ушли на трое суток, а ожидание не вышло по простою: дождалось сигнала (В2).
    assert code == sub_wait.EXIT_SIGNALS and len(core.seen) == 3
    assert calls[0] == (4242, CONV, "/work/iip", 100)
    assert core.seen[0][3]["session"] == {"pid": 100, "proc_start": 5000, "session_id": f"codex:{CONV}"}
    assert out.startswith("Сигнал хаба по подписке #17 (проект 3): новое — карточек 1.")
    for bad in ("перезапусти", "Снова ждать", "Bash", COMMAND):
        assert bad not in out, bad


def test_engine_gone_exit_5(tmp_path):
    code, out, _ = _run(tmp_path, Core([]), session=reg.RegistryError(reg.GONE, "нет"))
    assert code == sub_wait.EXIT_NO_SESSION and "завершилась" in out


def test_leave_by_ext_session(tmp_path):
    core = Core([(200, {"items": [{"id": 17, "mine": True}], "channels": []}), (200, {"sub": {"id": 17}})])
    out = io.StringIO()
    code = sub_wait.run_leave(
        profile="all",
        url=URL,
        config_dir=_profile(tmp_path),
        oc_session=None,
        oc_dir=None,
        transport=httpx.MockTransport(core.handler),
        out=out,
        err=io.StringIO(),
        ext_session=CONV,
        ext_dir="/work/iip",
        ext_pid=100,
        find_ext=lambda _start, _sid, _dir, *, ext_pid=None: S_CX,
    )
    assert code == 0 and "снято подписок сессии: 1" in out.getvalue()
    end = {
        "session": {"pid": 100, "proc_start": 5000, "session_id": f"codex:{CONV}"},
        "reason": "unsubscribed",
    }
    assert core.seen[1] == ("POST", "/api/v1/subs/17/end", {}, end)


def test_cli_parses_ext_flags_and_leave_needs_a_session(monkeypatch, tmp_path, capsys):
    seen = {}

    def fake_run_wait(sub_id, **kw):
        seen["wait"] = (sub_id, kw["ext_session"], kw["ext_dir"], kw["ext_pid"])
        return 0

    def fake_run_leave(**kw):
        seen["leave"] = (kw["ext_session"], kw["ext_dir"], kw["ext_pid"], kw["oc_session"])
        return 0

    monkeypatch.setattr(sub_wait, "run_wait", fake_run_wait)
    monkeypatch.setattr(sub_wait, "run_leave", fake_run_leave)
    monkeypatch.setenv("HUB_CONFIG_DIR", str(tmp_path))
    ext = ["--ext-session", CONV, "--ext-dir", "/w", "--ext-pid", "100"]
    for argv in (
        ["hub", "sub", "wait", "17", "--profile", "all", *ext],
        ["hub", "sub", "leave", "--profile", "all", *ext],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit) as exc:
            hub_cli.main()
        assert exc.value.code == 0
    assert seen == {"wait": (17, CONV, "/w", 100), "leave": (CONV, "/w", 100, None)}
    for argv in (
        ["hub", "sub", "leave", "--profile", "all"],
        ["hub", "sub", "leave", "--profile", "all", "--oc-session", "ses_x", "--oc-dir", "/w", *ext],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit) as exc:
            hub_cli.main()
        assert exc.value.code == sub_wait.EXIT_DENIED
