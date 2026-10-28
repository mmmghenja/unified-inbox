"""Ф16-4 (docs/architecture.md): `hub sub wait … --oc-session S --oc-dir D`
(ожидание, которое держит плагин хаба для сессии OpenCode) и `hub sub leave` (плагин на `session.deleted`).
Ядро — подменный транспорт; сессия — подменная `find_oc`."""

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
OC_A = "ses_AAAAAAAAAA1"
S_OC = reg.SessionInfo(100, 5000, OC_A, "/work/iip", None, None, "opencode")


class Core:
    def __init__(self, responses):
        self.responses = list(responses)
        self.seen: list[tuple[str, str, dict, dict | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.seen.append((request.method, request.url.path, dict(request.url.params), body))
        status, payload = self.responses.pop(0)
        return httpx.Response(status, json=payload)


def _profile(tmp_path, token="hubt_good"):
    config_dir = tmp_path / "config"
    profile_mod.write_profile(config_dir, "all", token=token, meta={"token_id": 1})
    return config_dir


def _run(tmp_path, core, *, channels=False, session=S_OC):
    config_dir = _profile(tmp_path)
    calls = []

    def find_oc(start, sid, oc_dir):
        calls.append((start, sid, oc_dir))
        if isinstance(session, BaseException):
            raise session
        return session

    out, err = io.StringIO(), io.StringIO()
    code = sub_wait.run_wait(
        None if channels else 17,
        channels=channels,
        profile="all",
        url=URL,
        config_dir=config_dir,
        command=COMMAND,
        transport=httpx.MockTransport(core.handler),
        find=lambda _pid: pytest.fail("реестр Claude Code для сессии OpenCode не читается"),
        getppid=lambda: 4242,
        sleep=lambda _s: None,
        out=out,
        err=err,
        oc_session=OC_A,
        oc_dir="/work/iip",
        find_oc=find_oc,
    )
    return code, out.getvalue(), calls


def _card(item_id=845, label="urgent"):
    return {"item_id": item_id, "channel_id": 152, "account_id": 5, "label": label, "msg_count": 2}


def test_signal_text_without_restart_and_command(tmp_path):
    core = Core([(200, {"sub_id": 17, "project_id": 3, "signals": [_card()]})])
    code, out, calls = _run(tmp_path, core)
    assert code == sub_wait.EXIT_SIGNALS
    assert calls[0] == (4242, OC_A, "/work/iip")
    assert core.seen[0][3]["session"] == {"pid": 100, "proc_start": 5000, "session_id": OC_A}
    assert out.startswith("Сигнал хаба по подписке #17 (проект 3): новое — карточек 1.")
    assert "карточка #845 · срочно · беседа #152 · аккаунт 5 · сообщений 2" in out
    assert 'Разбери; разобранное закрой hub_triage_mark(item_ids=[…], status="done")' in out
    for bad in ("перезапусти", "Снова ждать", "Bash", COMMAND, "Claude Code"):
        assert bad not in out, bad


def test_messages_and_channel_signals_without_restart(tmp_path):
    msg = {"kind": "messages", "channel_id": 152, "account_id": 5, "msg_count": 3, "message_id": 99}
    code, out, _ = _run(tmp_path, Core([(200, {"sub_id": 17, "project_id": 3, "signals": [msg]})]))
    assert code == 0 and "бесед с новыми сообщениями 1" in out and "перезапусти" not in out
    assert "Снова ждать" not in out
    chan = {"sub_id": 4, "channel_id": 152, "account_id": 5, "msg_count": 1, "message_id": 99}
    code, out, _ = _run(tmp_path, Core([(200, {"signals": [chan]})]), channels=True)
    assert code == 0 and out.startswith("Сигнал хаба: новое в беседах (1).")
    assert "запусти ожидание" not in out and "Снова ждать" not in out


@pytest.mark.parametrize(
    ("response", "code"),
    [
        ((410, {"detail": {"end_reason": "engine_switched"}}), sub_wait.EXIT_ENDED),
        ((409, {"detail": "superseded"}), sub_wait.EXIT_SUPERSEDED),
        ((403, {"detail": "подписка другой сессии"}), sub_wait.EXIT_DENIED),
        (
            (200, {"sub_id": 17, "project_id": 3, "signals": [{"label": "что-то новое"}]}),
            sub_wait.EXIT_UNKNOWN_SIGNALS,
        ),
    ],
)
def test_exit_codes_same_texts_neutral(tmp_path, response, code):
    got, out, _ = _run(tmp_path, Core([response]))
    assert got == code
    assert "НЕ перезапускай" not in out and "Claude Code" not in out
    if code == sub_wait.EXIT_ENDED:
        assert "хаб переключён на другой движок" in out and sub_wait.NO_RESTART_OC in out


def test_session_gone_exit_5(tmp_path):
    code, out, _ = _run(tmp_path, Core([]), session=reg.RegistryError(reg.GONE, "нет"))
    assert code == sub_wait.EXIT_NO_SESSION and "завершилась" in out


def test_claude_texts_unchanged():
    """Без `oc` — текст Claude Code прежний (перезапуск и команда)."""
    text = sub_wait.signals_text(17, 3, [{**_card(), "label": "reply"}], COMMAND)
    assert "Сначала снова запусти ожидание" in text and text.endswith(
        f"Снова ждать (Bash, в фоне, timeout=7200000): {COMMAND}"
    )


# --- hub sub leave --------------------------------------------------------------------------------------


def _leave(tmp_path, core, *, session=S_OC):
    config_dir = _profile(tmp_path)
    out, err = io.StringIO(), io.StringIO()

    def find_oc(_start, _sid, _dir):
        if isinstance(session, BaseException):
            raise session
        return session

    code = sub_wait.run_leave(
        profile="all",
        url=URL,
        config_dir=config_dir,
        oc_session=OC_A,
        oc_dir="/work/iip",
        transport=httpx.MockTransport(core.handler),
        find_oc=find_oc,
        getppid=lambda: 4242,
        out=out,
        err=err,
    )
    return code, out.getvalue()


def test_leave_ends_only_mine(tmp_path):
    body = {
        "items": [{"id": 17, "mine": True}, {"id": 18, "mine": False}],
        "channels": [{"id": 4, "mine": True}, {"id": 5, "mine": False}],
    }
    core = Core([(200, body), (200, {"sub": {"id": 17}}), (200, {"items": [{"id": 4}]})])
    code, out = _leave(tmp_path, core)
    assert code == 0 and "снято подписок сессии: 2" in out
    me = {"pid": "100", "proc_start": "5000", "session_id": OC_A}
    assert core.seen[0][:3] == ("GET", "/api/v1/subs", {"live": "1", **me})
    end = {"session": {"pid": 100, "proc_start": 5000, "session_id": OC_A}, "reason": "unsubscribed"}
    assert core.seen[1] == ("POST", "/api/v1/subs/17/end", {}, end)
    assert core.seen[2] == ("POST", "/api/v1/chan-subs/end", {}, end)
    assert len(core.seen) == 3


def test_leave_nothing_and_errors(tmp_path):
    code, out = _leave(tmp_path, Core([(200, {"items": [], "channels": []})]))
    assert code == 0 and "снято подписок сессии: 0" in out
    code, _ = _leave(tmp_path, Core([(403, {"detail": "x"})]))
    assert code == sub_wait.EXIT_DENIED
    code, _ = _leave(tmp_path, Core([]), session=reg.RegistryError(reg.GONE, "нет"))
    assert code == sub_wait.EXIT_NO_SESSION


def test_cli_parses_oc_flags(monkeypatch, tmp_path):
    seen = {}

    def fake_run_wait(sub_id, **kw):
        seen["wait"] = (sub_id, kw["oc_session"], kw["oc_dir"], kw["profile"])
        return 0

    def fake_run_leave(**kw):
        seen["leave"] = (kw["oc_session"], kw["oc_dir"], kw["profile"], kw["url"])
        return 0

    monkeypatch.setattr(sub_wait, "run_wait", fake_run_wait)
    monkeypatch.setattr(sub_wait, "run_leave", fake_run_leave)
    monkeypatch.setenv("HUB_CONFIG_DIR", str(tmp_path))
    for argv in (
        ["hub", "sub", "wait", "17", "--profile", "all", "--oc-session", OC_A, "--oc-dir", "/w"],
        ["hub", "sub", "leave", "--oc-session", OC_A, "--oc-dir", "/w", "--profile", "all"],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit) as exc:
            hub_cli.main()
        assert exc.value.code == 0
    assert seen["wait"] == (17, OC_A, "/w", "all")
    assert seen["leave"] == (OC_A, "/w", "all", URL)


def test_ended_by_owner_oc_no_resubscribe_hint(tmp_path):
    got, out, _ = _run(tmp_path, Core([(410, {"detail": {"end_reason": "ended_by_owner"}})]))
    assert got == sub_wait.EXIT_ENDED
    assert sub_wait.NO_RESUB_OWNER in out and sub_wait.NO_RESTART_OC not in out
