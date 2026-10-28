"""Ф92-1 B4 (docs/architecture.md): `hub_subscribe(role=…)` не молчит о
подписках на беседы — «вернуть нечего (нет прежней сессии роли)», «от прежней подписки роли #N возвращать
нечего»; вернувшиеся и невернувшиеся — как в Ф32-1. Ядро — подменный транспорт. B3: `hub mcp setup`
отзывает прежний токен профиля как перевыпущенный."""

import json

import httpx

from hub.mcp_server import profile as profile_mod
from hub.mcp_server import setup as mcp_setup

from .conftest import text_of
from .test_f32_mcp_restore import CHAN_WAIT, ROLE_SUB
from .test_mcp_chan_subs import Core, _call, _server

NO_SOURCE = "Подписки на беседы: вернуть нечего (нет прежней сессии роли)."


def _core(**extra) -> Core:
    payload = {"sub": ROLE_SUB, "created": True, "backfilled": 0, "neighbours": [], **extra}
    return Core({("POST", "/subs"): (200, payload)})


async def test_no_source_line(config_dir, make_server):
    core = _core(restore_blocked="no_source")
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscribe", role="маркетолог"))
    assert NO_SOURCE in text
    assert "не возвращаю" not in text and "Вернулись" not in text and CHAN_WAIT not in text
    assert [seen[1] for seen in core.seen] == ["/subs"]


async def test_source_with_nothing_line(config_dir, make_server):
    server, _hub = _server(make_server, config_dir, _core(restore_source=17))
    text = text_of(await _call(server, "hub_subscribe", role="маркетолог"))
    assert "Подписки на беседы: от прежней подписки роли #17 возвращать нечего" in text
    assert NO_SOURCE not in text and CHAN_WAIT not in text


async def test_without_role_fields_silent(config_dir, make_server):
    """Ядро ничего не сказало про беседы (подписка без роли, `restore_channels=False`) — строк нет."""
    server, _hub = _server(make_server, config_dir, _core())
    text = text_of(await _call(server, "hub_subscribe"))
    assert "Подписки на беседы" not in text


def test_setup_reissue_revokes_with_replaced_by(config_dir):
    """B3: `hub mcp setup` при перевыпуске отзывает прежний токен профиля с `replaced_by` — ядро завершит его
    подписки `token_reissued` (возвращаемо), а не `token_revoked` (насовсем)."""
    profile_mod.write_profile(config_dir, "all", token="t", meta={"token_id": 6, "projects": None})
    calls: list = []

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"null")
        calls.append((request.method, request.url.path, body))
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": 6, "name": "mcp:all", "revoked": False}])
        if request.url.path.endswith("/admin/tokens"):
            return httpx.Response(200, json={"token": "new-secret", "token_id": 207})
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(_handler)
    with httpx.Client(transport=transport, base_url="http://hub-internal/internal/v1") as admin:
        text = mcp_setup.run_setup(admin, config_dir=config_dir, profile="all", hub_path="/x/hub")
    assert calls[-1] == ("POST", "/internal/v1/admin/tokens/6/revoke", {"replaced_by": 207})
    assert "id=6) отозван" in text
