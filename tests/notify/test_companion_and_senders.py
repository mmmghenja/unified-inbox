"""Ф33-2: спутник `hub notify`, запись в ядро из служб, путь `hub watch` при живом и мёртвом ядре."""

import json

import httpx

from hub import watch
from hub.notify import commands, files
from hub.notify.service import DOWN_AFTER_S, EXIT_UNAUTHORIZED, Companion


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def _companion(handler, shown, clock=None):
    clock = clock or Clock()
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://core/api/v1")
    return (
        Companion(
            client=client,
            token="tok",
            show=lambda *a: shown.append(a),
            clock=clock,
            sleep=clock.sleep,
        ),
        clock,
    )


def _ok(items, last, epoch="e1", reset=False):
    return httpx.Response(200, json={"items": items, "last": last, "epoch": epoch, "reset": reset})


def _item(i, title="T", urgency="critical"):
    return {"id": i, "ts": 1, "title": title, "text": "x", "urgency": urgency}


# --- спутник ------------------------------------------------------------------------------------------------


def test_companion_baseline_shows_items_with_urgency_and_tracks_after() -> None:
    seen, shown = [], []
    replies = iter([_ok([], 7), _ok([_item(8, "A"), _item(9, "B", "normal")], 9), _ok([], 9)])

    def handler(request):
        seen.append((dict(request.url.params), request.headers["authorization"]))
        return next(replies)

    comp, _ = _companion(handler, shown)
    for _ in range(3):
        assert comp.step() is None
    assert shown == [("A", "x", "critical"), ("B", "x", "normal")]
    assert "after" not in seen[0][0] and seen[1][0]["after"] == "7" and seen[2][0]["after"] == "9"
    assert seen[1][0]["epoch"] == "e1" and seen[0][1] == "Bearer tok"


def test_companion_resets_after_when_core_restarted() -> None:
    shown = []
    replies = iter(
        [_ok([], 50, "e1"), _ok([_item(1, "after-restart")], 1, "e2", reset=True), _ok([], 1, "e2")]
    )
    params = []

    def handler(request):
        params.append(dict(request.url.params))
        return next(replies)

    comp, _ = _companion(handler, shown)
    for _ in range(3):
        comp.step()
    assert shown == [("after-restart", "x", "critical")]
    assert params[1]["after"] == "50" and params[2]["after"] == "1" and params[2]["epoch"] == "e2"


def test_companion_down_over_two_minutes_one_message_then_recovery() -> None:
    shown = []
    state = {"up": False}

    def handler(request):
        if not state["up"]:
            raise httpx.ConnectError("down")
        return _ok([], 1)

    comp, clock = _companion(handler, shown)
    while clock.t < DOWN_AFTER_S - 10:
        comp.step()
    assert shown == []  # ещё меньше 2 минут
    for _ in range(10):
        comp.step()
    assert [s[0] for s in shown] == ["hub: хаб недоступен"]  # одно, не по разу на попытку
    state["up"] = True
    comp.step()
    comp.step()
    assert [s[0] for s in shown] == ["hub: хаб недоступен", "hub: хаб снова на связи"]


def test_companion_short_outage_is_silent() -> None:
    shown = []
    replies = iter([httpx.ConnectError("x"), _ok([], 1), _ok([], 1)])

    def handler(request):
        r = next(replies)
        if isinstance(r, Exception):
            raise r
        return r

    comp, _ = _companion(handler, shown)
    for _ in range(3):
        comp.step()
    assert shown == []


def test_companion_revoked_token_exits_78() -> None:
    comp, _ = _companion(lambda request: httpx.Response(401), [])
    assert comp.step() == EXIT_UNAUTHORIZED


# --- setup/status -------------------------------------------------------------------------------------------


class _FakeAdmin:
    def __init__(self, tokens=(), first_id=9):
        self.tokens = list(tokens)
        self.posted = []
        self.first_id = first_id

    def get(self, path):
        return httpx.Response(200, json=self.tokens, request=httpx.Request("GET", "http://x" + path))

    def post(self, path, json=None):
        self.posted.append((path, json))
        if path == "/admin/tokens":
            return httpx.Response(200, json={"token": "hubt_secret_value", "token_id": self.first_id})
        return httpx.Response(200, json={})


def test_setup_writes_token_0600_and_revokes_old(tmp_path) -> None:
    admin = _FakeAdmin()
    text = commands.run_setup(admin, config_dir=tmp_path)
    path = files.token_path(tmp_path)
    assert path.name == "notify.token" and path.read_text().strip() == "hubt_secret_value"
    assert path.stat().st_mode & 0o777 == 0o600
    assert "hubt_secret_value" not in text and "hub-notify.service" in text
    assert admin.posted[0][1]["scopes"] == ["notify"] and admin.posted[0][1]["kind"] == "background"
    admin2 = _FakeAdmin(tokens=[{"id": 9, "name": "notify:companion", "revoked": False}], first_id=10)
    commands.run_setup(admin2, config_dir=tmp_path)
    assert ("/admin/tokens/9/revoke", None) in admin2.posted


def test_status_without_token_and_with_core(tmp_path) -> None:
    assert "нет" in commands.status(tmp_path)
    files.write_secret(files.token_path(tmp_path), "tok")
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"last": 4}))
    assert "4" in commands.status(tmp_path, base_url="http://core/api/v1", transport=transport)


# --- службы пишут в ядро ------------------------------------------------------------------------------------


def _capture_uds(monkeypatch, status=200):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, json={"id": 1})

    monkeypatch.setattr(watch.httpx, "HTTPTransport", lambda uds: httpx.MockTransport(handler))
    return seen


def test_notify_via_core_posts_to_internal_socket(monkeypatch, tmp_path) -> None:
    seen = _capture_uds(monkeypatch)
    assert watch.notify_via_core(
        "t", "x", urgency="normal", config_dir=tmp_path, data_dir=tmp_path, admin_secret="huba_k"
    )
    req = seen[0]
    assert req.url.path == "/internal/v1/notify" and req.headers["authorization"] == "Bearer huba_k"
    assert json.loads(req.content) == {"title": "t", "text": "x", "urgency": "normal"}


def test_notify_via_core_unavailable_goes_to_stderr_without_text_when_quiet(
    monkeypatch, tmp_path, capsys
) -> None:
    def boom(uds):
        raise httpx.ConnectError("no socket")

    monkeypatch.setattr(watch.httpx, "HTTPTransport", boom)
    assert not watch.notify_via_core(
        "секрет-заголовок",
        "секрет-текст",
        quiet=True,
        config_dir=tmp_path,
        data_dir=tmp_path,
        admin_secret="k",
    )
    err = capsys.readouterr().err
    assert "секрет" not in err and "ядро не приняло" in err


def _patch_post_core(monkeypatch, log, reply):
    monkeypatch.setattr(watch, "_post_core", lambda t, x, **kw: log.append(("core", t, x)) or reply)
    monkeypatch.setattr(watch, "notify_send", lambda t, x, **kw: log.append(("send", t, x)))


def test_agent_and_work_notifiers_core_only_when_companion_listens(monkeypatch) -> None:
    from hub.agent import main as agent_main
    from hub.work import main as work_main

    log = []
    _patch_post_core(monkeypatch, log, {"id": 1, "listened": True})
    agent_main.default_notifier("t", "x", "normal")
    work_main.default_notifier("t2", "x2", "critical")
    assert log == [("core", "t", "x"), ("core", "t2", "x2")]  # прямого notify-send нет: дублей нет


def test_agent_and_work_notifiers_also_direct_when_nobody_listens_or_core_down(monkeypatch) -> None:
    from hub.agent import main as agent_main
    from hub.work import main as work_main

    for reply in ({"id": 1, "listened": False}, None):
        log = []
        _patch_post_core(monkeypatch, log, reply)
        agent_main.default_notifier("t", "x", "normal")
        work_main.default_notifier("t2", "x2", "critical")
        assert log == [("core", "t", "x"), ("send", "t", "x"), ("core", "t2", "x2"), ("send", "t2", "x2")]


# --- hub watch ----------------------------------------------------------------------------------------------


def _run_watch(monkeypatch, tmp_path, snapshot, *, core_ok=True, listened=True):
    log = []
    monkeypatch.setattr(watch, "gather_snapshot", lambda **kw: snapshot)
    _patch_post_core(monkeypatch, log, {"id": 1, "listened": listened} if core_ok else None)
    watch.run_watch(port=1, data_dir=tmp_path, config_dir=tmp_path, admin_secret="k", now_ms=1_000)
    return log


def test_watch_with_living_core_writes_to_core_only(monkeypatch, tmp_path) -> None:
    snap = {"core_down": False, "now": 1_000, "accounts": [], "dead_letter_total": 3, "outbox_unknown": 0}
    log = _run_watch(monkeypatch, tmp_path, snap)
    assert [e[0] for e in log] == ["core"] and "dead_letter" in log[0][1]


def test_watch_with_living_core_that_refuses_falls_back_to_notify_send(monkeypatch, tmp_path) -> None:
    snap = {"core_down": False, "now": 1_000, "accounts": [], "dead_letter_total": 3, "outbox_unknown": 0}
    log = _run_watch(monkeypatch, tmp_path, snap, core_ok=False)
    assert [e[0] for e in log] == ["core", "send"]  # Telegram не нужен: ядро живо


def test_watch_with_living_core_nobody_listening_also_notify_send(monkeypatch, tmp_path) -> None:
    snap = {"core_down": False, "now": 1_000, "accounts": [], "dead_letter_total": 3, "outbox_unknown": 0}
    log = _run_watch(monkeypatch, tmp_path, snap, listened=False)
    assert [e[0] for e in log] == ["core", "send"]


def test_watch_with_dead_core_notify_send_and_one_telegram_line(monkeypatch, tmp_path) -> None:
    log = _run_watch(monkeypatch, tmp_path, {"core_down": True})
    assert log[0][0] == "send"
    # второй прогон с тем же состоянием: антиспам молчит -> ни второго сообщения, ни второй строки
    log2 = _run_watch(monkeypatch, tmp_path, {"core_down": True})
    assert log2 == []


def test_watch_recovery_is_one_message_each_way(monkeypatch, tmp_path) -> None:
    _run_watch(monkeypatch, tmp_path, {"core_down": True})
    snap = {"core_down": False, "now": 1_000, "accounts": [], "dead_letter_total": 0, "outbox_unknown": 0}
    log = _run_watch(monkeypatch, tmp_path, snap)
    assert [(e[0], e[2]) for e in log] == [("core", "восстановилось")]
