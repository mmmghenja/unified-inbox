"""Ф60-1, часть A: `hub listen setup|hint|status` (токен — по образцу `hub transcribe setup`). Админ-сокет и
ядро — поддельные (`httpx.MockTransport`)."""

import json
import stat

import httpx

from hub.listen import commands, files


class FakeAdmin:
    def __init__(self, tokens=None, first_id=41) -> None:
        self.first_id = first_id
        self.created: list[dict] = []
        self.revoked: list[int] = []
        self.tokens = tokens or []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/admin/tokens":
            return httpx.Response(200, json=self.tokens)
        if request.method == "POST" and request.url.path == "/admin/tokens":
            body = json.loads(request.content)
            self.created.append(body)
            return httpx.Response(
                200, json={"token": "hub_secret_value", "token_id": self.first_id + len(self.created) - 1}
            )
        if request.url.path.endswith("/revoke"):
            self.revoked.append(int(request.url.path.split("/")[-2]))
            return httpx.Response(200, json={})
        return httpx.Response(404, json={})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler), base_url="http://admin")


def test_setup_writes_the_token_0600_and_revokes_the_previous(tmp_path):
    cfg, data = tmp_path / "cfg", tmp_path / "data"
    admin = FakeAdmin()
    text = commands.run_setup(admin.client(), config_dir=cfg, data_dir=data)
    assert admin.created == [
        {
            "name": "listen:recorder",
            "kind": "background",
            "scopes": ["listen"],
            "project_ids": None,
            "ttl_days": None,
        }
    ]
    path = files.token_path(cfg)
    assert path == cfg / "listen" / "token"
    assert path.read_text().strip() == "hub_secret_value"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(files.data_service_dir(data).stat().st_mode) == 0o700
    assert "hub_secret_value" not in text and "hub-listen.service" in text
    # Повторный setup отзывает прежний токен (только свой — по имени и номеру из token.json).
    assert json.loads(files.token_meta_path(cfg).read_text()) == {"token_id": 41}
    admin2 = FakeAdmin(tokens=[{"id": 41, "name": "listen:recorder", "revoked": False}], first_id=42)
    text2 = commands.run_setup(admin2.client(), config_dir=cfg, data_dir=data)
    assert admin2.revoked == [41] and "Прежний токен (id=41) отозван." in text2
    assert json.loads(files.token_meta_path(cfg).read_text()) == {"token_id": 42}
    # Чужой токен с тем же номером (другое имя) не отзывается.
    admin3 = FakeAdmin(tokens=[{"id": 42, "name": "hub-all", "revoked": False}], first_id=43)
    commands.run_setup(admin3.client(), config_dir=cfg, data_dir=data)
    assert admin3.revoked == []


def _core(status: int, body: dict | None = None):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body or {})

    return httpx.MockTransport(handler), seen


def test_hint_exit_codes(tmp_path):
    cfg = tmp_path / "cfg"
    assert commands.hint(config_dir=cfg)[0] == commands.EXIT_NO_TOKEN
    files.write_secret(files.token_path(cfg), "hub_tok")
    transport, seen = _core(200, {"queued": False})
    assert commands.hint(config_dir=cfg, transport=transport) == (0, commands.TEXT_HINT_OK)
    assert seen[0].url.path == "/api/v1/listen/hint" and seen[0].headers["authorization"] == "Bearer hub_tok"
    transport, _ = _core(200, {"queued": True})
    assert commands.hint(config_dir=cfg, transport=transport) == (0, commands.TEXT_HINT_QUEUED)
    transport, _ = _core(404)
    assert commands.hint(config_dir=cfg, transport=transport) == (2, commands.TEXT_NO_INTERVIEW)
    transport, _ = _core(500)
    assert commands.hint(config_dir=cfg, transport=transport)[0] == 1


def test_status_text_has_no_line_text(tmp_path):
    cfg = tmp_path / "cfg"
    data = {
        "interview": {
            "id": 3,
            "session_id": 9,
            "mode": "live",
            "them_lang": "en-US",
            "status": "paused",
            "started_ts": 1_700_000_000_000,
            "lines": 12,
            "queued_says": 1,
            "reason": "ядро перезапущено",
            "warn": None,
            "wait_reason": None,
        },
        "listener_seen_ms": 1_700_000_000_000,
        "listener_alive": True,
        "now_ms": 1_700_000_003_000,
    }
    text = commands.status_text(cfg, data)
    assert "Токен службы: нет" in text and "жива, опрашивала 3 с назад" in text
    assert "Интервью #3 в сессии #9: на паузе, по ходу" in text and "Причина: ядро перезапущено." in text
    assert "Интервью не идёт." in commands.status_text(cfg, {"interview": None, "now_ms": 1})
    assert "Ядро не отвечает" in commands.status_text(cfg, None)
