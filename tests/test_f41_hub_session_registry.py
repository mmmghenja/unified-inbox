"""Ф41-2а (docs/architecture.md + П1; раздел 5 п.9): личность сессии хаба по
`HUB_SESSION` (`session_registry`) и отказ `hub sub wait` (`sub_wait.run_wait`, код 2)."""

import io

import httpx
import pytest

from hub import session_registry as reg
from hub import sub_wait
from hub.core import held


def test_no_hub_session_env_is_none():
    assert reg.hub_session_id({}) is None
    assert reg.hub_session_id({"HUB_SESSION": ""}) is None
    assert reg.hub_session_id({"HUB_SESSION": "  "}) is None
    assert reg.find_hub_session({}) is None
    assert reg.find_hub_session({"OPENCODE_PID": "4096", "HUB_WORK": "1"}) is None


def test_hub_session_identity_is_conditional_triple():
    env = {"HUB_SESSION": "12", "OPENCODE_PID": "4096", "CLAUDE_CONFIG_DIR": "/nonexistent"}
    info = reg.find_hub_session(env, getcwd=lambda: "/work/iip")
    assert info == reg.SessionInfo(-12, 0, "hub:12", "/work/iip", None, None, "hub")
    # Та же тройка, что у ядра, и она проходит общий валидатор ядра.
    assert (info.pid, info.proc_start, info.session_id) == held.triple(12)
    assert held.triple_error(info.pid, info.proc_start, info.session_id, info.kind) is None
    assert info.session() == {"pid": -12, "proc_start": 0, "session_id": "hub:12"}
    assert info.session_in()["kind"] == "hub"
    assert not reg.in_work_session(env)  # хост постоянных сессий `HUB_WORK` не ставит


@pytest.mark.parametrize("raw", ["0", "-5", "abc", "1.5", "٣", str(2**31)])
def test_bad_hub_session_env_is_error_not_silent_choice(raw):
    with pytest.raises(reg.RegistryError) as exc:
        reg.hub_session_id({"HUB_SESSION": raw})
    assert exc.value.kind == reg.GONE and "HUB_SESSION" in exc.value.message


def test_hub_session_with_hub_work_is_error():
    with pytest.raises(reg.RegistryError) as exc:
        reg.find_hub_session({"HUB_SESSION": "3", "HUB_WORK": "1"})
    assert "HUB_SESSION и HUB_WORK" in exc.value.message


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        ({"HUB_SESSION": "3"}, "сессии хаба сигналы подаёт хост"),
        # Причина названа настоящая — не «сигналы подаёт хост» (ревью, мелкое 2); код выхода тот же.
        ({"HUB_SESSION": "3", "HUB_WORK": "1"}, "HUB_SESSION и HUB_WORK заданы вместе"),
        ({"HUB_SESSION": "x"}, "HUB_SESSION: нужен номер сессии хаба"),
    ],
)
@pytest.mark.parametrize("channels", [False, True])
def test_sub_wait_from_hub_session_exits_2_without_requests(tmp_path, env, reason, channels):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"запросов к ядру быть не должно: {request.url}")

    def find(_pid):
        raise AssertionError("по предкам проверять нечего")

    out, err = io.StringIO(), io.StringIO()
    code = sub_wait.run_wait(
        None if channels else 17,
        channels=channels,
        profile="all",
        url="http://127.0.0.1:1/api/v1",
        config_dir=tmp_path,
        command="hub sub wait 17 --profile all",
        transport=httpx.MockTransport(handler),
        find=find,
        getppid=lambda: 4242,
        sleep=lambda _s: None,
        out=out,
        err=err,
        environ=env,
    )
    assert code == sub_wait.EXIT_DENIED == 2
    assert reason in out.getvalue()
    assert ("сигналы подаёт хост" in out.getvalue()) is (env == {"HUB_SESSION": "3"})
    assert "не перезапускай" in out.getvalue()


def test_sub_wait_without_hub_session_env_goes_on(tmp_path):
    """Без `HUB_SESSION` — прежний путь: доходит до проверки профиля."""
    out = io.StringIO()
    code = sub_wait.run_wait(
        17,
        profile=None,
        url="http://127.0.0.1:1/api/v1",
        config_dir=tmp_path,
        command="x",
        out=out,
        err=io.StringIO(),
        environ={},
    )
    assert code == sub_wait.EXIT_DENIED and "не задан профиль" in out.getvalue()
