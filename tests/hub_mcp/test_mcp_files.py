"""hub-mcp: файлы в черновиках от сессий (Ф14-3, `docs/architecture.md` и C, тесты
F 10–14 и поправки П1, П6, П7, П11, П12). Ядро — в процессе, MCP-клиент — в памяти; CLI `hub mcp attach` —
через `httpx.MockTransport` и против настоящего ядра."""

import hashlib
import json
import os
import threading

import httpx
import pytest
from httpx import ASGITransport
from mcp import Client

from hub.core import media
from hub.mcp_server import files as files_mod
from hub.mcp_server import profile as profile_mod
from hub.mcp_server import server as mcp_server
from hub.mcp_server import setup as mcp_setup

from .conftest import insert_channel, insert_message, insert_project, make_token, text_of
from .test_mcp_cli import _run_cli, real_core  # noqa: F401 — фикстура ядра дочерним процессом

MIB = 1024 * 1024


def _pdf(tag: str) -> bytes:
    return b"%PDF-1.7\n" + tag.encode() + b"y" * 50


def _write(path, data: bytes = b"%PDF-1.7\nhello") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


# --- F10 + П1, П6, П7: проверка пути (без ядра) ------------------------------------------------------------


@pytest.fixture
def workdir(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return root


def _refused(path: str, workdir) -> str:
    with pytest.raises(files_mod.FileRefused) as info:
        opened = files_mod.open_local(path, str(workdir))
        files_mod.close_all([opened])
    return str(info.value)


def _ok(path: str, workdir) -> files_mod.LocalFile:
    opened = files_mod.open_local(path, str(workdir))
    files_mod.close_all([opened])
    return opened


def test_10_inside_workdir_ok(workdir):
    got = _ok(_write(workdir / "out" / "a.pdf"), workdir)
    assert got.name == "a.pdf"
    assert got.size == len(b"%PDF-1.7\nhello")


def test_p1_relative_path_refused(workdir):
    _write(workdir / "out" / "a.pdf")
    assert "абсолютный" in _refused("out/a.pdf", workdir)


def test_10_escape_and_symlink_outside_refused(workdir, tmp_path):
    outside = _write(tmp_path / "secret.pdf")
    assert "вне рабочей папки" in _refused(str(workdir / ".." / "secret.pdf"), workdir)
    (workdir / "link.pdf").symlink_to(outside)
    assert "вне рабочей папки" in _refused(str(workdir / "link.pdf"), workdir)
    assert "вне рабочей папки" in _refused(str(workdir), workdir)


def test_p6_symlink_inside_shows_real_name(workdir):
    _write(workdir / "out" / "настоящий.pdf")
    (workdir / "report.pdf").symlink_to(workdir / "out" / "настоящий.pdf")
    assert _ok(str(workdir / "report.pdf"), workdir).name == "настоящий.pdf"


@pytest.mark.parametrize(
    "rel",
    [
        ".git/config",
        ".env.local",
        ".env",
        "id_ed25519",
        "keys/x.pem",
        ".ssh/key.pdf",
        ".GIT/config",
        "ID_RSA.pub",
        "cert/Server.KEY",
        "vault.kdbx",
        "tg.session",
        ".claude/settings.json",
        ".claude/worktrees/wt/.env",
        ".claude/worktrees/wt/.git/HEAD",
        ".claude/worktrees/wt/id_rsa",
        ".claude/worktrees/.git/config",
        ".claude/worktrees/.ssh/a.pdf",
        ".config/app/a.pdf",
        ".local/share/a.pdf",
    ],
)
def test_p7_forbidden_segments(workdir, rel):
    assert "служебный" in _refused(_write(workdir / rel), workdir)


@pytest.mark.parametrize(
    "rel", [".claude/worktrees/wt/out/a.pdf", ".claude/worktrees/wt/a.pdf", "id_card.pdf", "docs/key.txt"]
)
def test_p7_allowed(workdir, rel):
    assert _ok(_write(workdir / rel), workdir).size > 0


def test_10_directory_empty_big_hardlink_refused(workdir):
    (workdir / "dir").mkdir()
    assert "не обычный файл" in _refused(str(workdir / "dir"), workdir)
    assert "пустой" in _refused(_write(workdir / "empty.pdf", b""), workdir)
    big = workdir / "big.bin"
    with open(big, "wb") as f:
        f.truncate(50 * MIB + 1)
    assert "50 МиБ" in _refused(str(big), workdir)
    one = _write(workdir / "one.pdf")
    os.link(one, workdir / "two.pdf")
    assert "жёстких ссылок" in _refused(one, workdir)


def test_p6_fifo_refused_without_hang(workdir):
    fifo = workdir / "pipe.pdf"
    os.mkfifo(fifo)
    result: dict = {}

    def _run():
        try:
            files_mod.open_local(str(fifo), str(workdir))
        except files_mod.FileRefused as exc:
            result["error"] = str(exc)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    assert "не обычный файл" in result["error"]


def test_10_no_workdir_only_media(workdir):
    path = _write(workdir / "a.pdf")
    with pytest.raises(files_mod.FileRefused, match="только media"):
        files_mod.open_local(path, None)
    assert files_mod.parse_ref("media:345") == files_mod.MediaRef(345)
    with pytest.raises(files_mod.FileRefused):
        files_mod.parse_ref("media:abc")


# --- обвязка сервера ----------------------------------------------------------------------------------------


class _Recording(httpx.AsyncBaseTransport):
    """ASGI-транспорт ядра, который помнит пути запросов (проверка «без запросов к ядру»)."""

    def __init__(self, app) -> None:
        self.inner = ASGITransport(app=app)
        self.calls: list[tuple[str, str]] = []

    async def handle_async_request(self, request):
        self.calls.append((request.method, request.url.path))
        return await self.inner.handle_async_request(request)

    def writes(self) -> list[str]:
        return [path for method, path in self.calls if method == "POST"]


def _account(db, source="telegram") -> int:
    config = {"idempotent_send": True, "pace_window": "00:00-24:00"}
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.account (source, db, config_json) VALUES (?, 'main', ?)",
                (source, json.dumps(config)),
            ).lastrowid
        )
    )


def _attach_profile(db, config_dir, profile="all", *, scopes=("read", "draft", "attach"), name=None):
    token, token_id = make_token(db, scopes=scopes, name=name or f"mcp:{profile}")
    profile_mod.write_profile(
        config_dir,
        profile,
        token=token,
        meta={"token_id": token_id, "projects": None, "scopes": list(scopes)},
    )
    return token_id


@pytest.fixture
def env(db, config_dir, tmp_path):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    project = insert_project(db, name="Umbrella", workdir=str(workdir))
    personal = insert_project(db, name="Личное")
    tg = _account(db)
    channel = insert_channel(db, tg, project_id=project, ext_id="c1", title="Клиент")
    personal_channel = insert_channel(db, tg, project_id=personal, ext_id="c2", title="Друг")
    _attach_profile(db, config_dir)
    return {
        "workdir": workdir,
        "project": project,
        "tg": tg,
        "channel": channel,
        "personal_channel": personal_channel,
    }


@pytest.fixture
def recording(public_app):
    return _Recording(public_app)


def _rows(db, sql, params=()):
    with db.reader() as conn:
        return conn.execute(sql, params).fetchall()


# --- F11: `hub_draft` с файлами ----------------------------------------------------------------------------


async def test_11_draft_two_files_in_order(db, env, make_server, recording):
    a = _write(env["workdir"] / "out" / "b-first.pdf", _pdf("1"))
    b = _write(env["workdir"] / "out" / "a-second.pdf", _pdf("2"))
    server, _hub = make_server("all", transport=recording)
    async with Client(server) as client:
        out = await client.call_tool("hub_draft", {"channel_id": env["channel"], "files": [a, b]})
    assert not out.is_error, text_of(out)
    text = text_of(out)
    assert "с файлами (2)" in text and "Ничего не отправлено" in text
    assert "b-first.pdf" in text and "a-second.pdf" in text
    assert text.index("b-first.pdf") < text.index("a-second.pdf")
    assert text.index("ДАННЫЕ") < text.index("b-first.pdf")  # П13: имена — в блоке данных
    assert recording.writes() == ["/api/v1/uploads", "/api/v1/uploads", "/api/v1/messages/send"]
    ((status, file_json),) = _rows(db, "SELECT status, file_json FROM main.outbox")
    assert status == "pending_approval"
    assert [f["name"] for f in json.loads(file_json)] == ["b-first.pdf", "a-second.pdf"]


async def test_11_draft_media_ref_forwards(db, clock, env, make_server, recording):
    message_id = insert_message(db, env["personal_channel"], ext_id="m1", ts=1000, kind="file")
    body = _pdf("fwd")
    sha = hashlib.sha256(body).hexdigest()
    path = media.file_path(db.data_dir, env["tg"], sha)
    media.ensure_dir(path.parent)
    path.write_bytes(body)
    media_id = db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.media (message_id, account_id, idx, kind, mime, size, name, status, sha256, "
            "stored_size, detected, created_ts) VALUES (?, ?, 0, 'document', 'application/pdf', ?, "
            "'скан.pdf', 'ready', ?, ?, 'pdf', 0)",
            (message_id, env["tg"], len(body), sha, len(body)),
        ).lastrowid
    )
    server, _hub = make_server("all", transport=recording)
    async with Client(server) as client:
        out = await client.call_tool(
            "hub_draft", {"channel_id": env["channel"], "text": "Пересылаю", "files": [f"media:{media_id}"]}
        )
    assert not out.is_error, text_of(out)
    assert "скан.pdf" in text_of(out)
    assert recording.writes() == ["/api/v1/uploads/from-media", "/api/v1/messages/send"]


async def test_10_refusals_make_no_core_writes(db, env, make_server, recording):
    secret = _write(env["workdir"] / ".env.local", b"TOKEN=1")
    ok = _write(env["workdir"] / "a.pdf")
    server, _hub = make_server("all", transport=recording)
    async with Client(server) as client:
        cases = [
            ({"channel_id": env["channel"], "files": [ok, secret]}, "служебный"),
            ({"channel_id": env["channel"], "files": ["a.pdf"]}, "абсолютный"),
            ({"channel_id": env["channel"], "files": [ok] * 11}, "не больше 10"),
            ({"channel_id": env["personal_channel"], "files": [ok]}, "только media"),
            # П11: подпись Telegram длиннее 1024 — отказ до загрузок
            ({"channel_id": env["channel"], "text": "я" * 1025, "files": [ok]}, "1024"),
        ]
        for args, needle in cases:
            out = await client.call_tool("hub_draft", args)
            assert out.is_error, args
            assert needle in text_of(out), text_of(out)
    assert recording.writes() == []
    assert _rows(db, "SELECT COUNT(*) FROM main.upload") == [(0,)]


async def test_11_core_refusal_text_passed_through(db, config_dir, env, make_server, recording):
    """Ошибка ядра (403 без `attach` у токена, хотя описание профиля его обещает) — текстом, черновика нет."""
    _attach_profile(db, config_dir, "stale", scopes=("read", "draft"))
    meta = profile_mod.read_meta(config_dir, "stale")
    profile_mod.write_meta(config_dir, "stale", {**meta, "scopes": ["read", "draft", "attach"]})
    ok = _write(env["workdir"] / "a.pdf")
    server, _hub = make_server("stale", transport=recording)
    async with Client(server) as client:
        out = await client.call_tool("hub_draft", {"channel_id": env["channel"], "files": [ok]})
    assert out.is_error
    assert "не загрузился" in text_of(out) and "нет права" in text_of(out)
    assert "/api/v1/messages/send" not in recording.writes()


# --- F12: `hub_draft_new` с файлом -------------------------------------------------------------------------


async def test_12_draft_new_with_file_hold_upload_send(db, env, make_server, recording):
    doc = _write(env["workdir"] / "kp.pdf", _pdf("kp"))
    server, _hub = make_server("all", transport=recording)
    async with Client(server) as client:
        out = await client.call_tool(
            "hub_draft_new",
            {
                "project_id": env["project"],
                "account_id": env["tg"],
                "to": "@petr_ivanov",
                "text": "Коммерческое во вложении",
                "files": [doc],
            },
        )
    assert not out.is_error, text_of(out)
    assert "kp.pdf" in text_of(out) and "по графику" in text_of(out)
    assert recording.writes() == ["/api/v1/drafts/new", "/api/v1/uploads", "/api/v1/messages/send"]
    ((channel_id, status, paced, file_json),) = _rows(
        db, "SELECT channel_id, status, paced, file_json FROM main.outbox"
    )
    assert (status, paced) == ("pending_approval", 1)
    assert [f["name"] for f in json.loads(file_json)] == ["kp.pdf"]
    ((ext_id,),) = _rows(db, "SELECT ext_id FROM main.channel WHERE id = ?", (channel_id,))
    assert ext_id.startswith("new:")


# --- F13 + П12: параметр `files` — только профилю с `attach` -----------------------------------------------


@pytest.mark.parametrize(
    ("profile", "scopes", "expected"),
    [
        ("all", ("read", "draft", "attach"), True),
        ("plain", ("read", "draft"), False),
    ],
)
async def test_13_files_param_only_with_attach(db, config_dir, make_server, profile, scopes, expected):
    token, token_id = make_token(db, scopes=("read", "draft"), name=f"mcp:{profile}")
    profile_mod.write_profile(
        config_dir,
        profile,
        token=token,
        meta={"token_id": token_id, "projects": None, "scopes": list(scopes)},
    )
    assert mcp_server.profile_has_attach(config_dir, profile) is expected
    server, _hub = make_server(profile)
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    for name in ("hub_draft", "hub_draft_new"):
        if name not in tools:
            continue  # оркестратору `hub_draft_new` не регистрируется вовсе
        assert ("files" in tools[name].input_schema["properties"]) is expected, (profile, name)


def test_13_tool_names_unchanged():
    assert "hub_draft" in mcp_server.tool_names("all")


async def test_hub_drafts_lists_files_in_data(db, env, make_server, recording):
    a = _write(env["workdir"] / "a.pdf", _pdf("a"))
    server, _hub = make_server("all", transport=recording)
    async with Client(server) as client:
        made = await client.call_tool("hub_draft", {"channel_id": env["channel"], "files": [a]})
        assert not made.is_error
        out = await client.call_tool("hub_drafts", {})
    text = text_of(out)
    assert "файлов: 1 (a.pdf)" in text
    assert text.index("ДАННЫЕ") < text.index("файлов: 1")


# --- F14: CLI `hub mcp attach` / `hub mcp setup --attach` ---------------------------------------------------


def _mock_admin(calls: list, *, status=200, scopes=("read", "draft", "organize", "attach")):
    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, json.loads(request.content or b"null")))
        return httpx.Response(status, json={"ok": True, "scopes": list(scopes)})

    return httpx.Client(transport=httpx.MockTransport(_handler), base_url="http://hub-internal/internal/v1")


def test_14_attach_on_uses_token_id_and_updates_meta(config_dir):
    profile_mod.write_profile(
        config_dir,
        "all",
        token="t",
        meta={"token_id": 6, "projects": None, "scopes": ["read", "draft", "organize"]},
    )
    calls: list = []
    with _mock_admin(calls) as admin:
        text = mcp_setup.run_attach(admin, config_dir=config_dir, profile="all", on=True)
    assert calls == [("POST", "/internal/v1/admin/tokens/6/attach", {"on": True})]
    assert profile_mod.read_meta(config_dir, "all")["scopes"] == ["read", "draft", "organize", "attach"]
    assert profile_mod.read_token(config_dir, "all") == "t"  # токен не перевыпущен
    assert "выдано" in text and "перезапусти" in text


def test_14_attach_core_refusal_keeps_meta(config_dir):
    profile_mod.write_profile(config_dir, "all", token="t", meta={"token_id": 6, "scopes": ["read", "draft"]})
    calls: list = []
    with _mock_admin(calls, status=409) as admin, pytest.raises(mcp_setup.SetupError, match="409"):
        mcp_setup.run_attach(admin, config_dir=config_dir, profile="all", on=True)
    assert profile_mod.read_meta(config_dir, "all")["scopes"] == ["read", "draft"]


def test_14_cli_attach_against_real_core(real_core, capsys):  # noqa: F811
    _data_dir, config_dir, _port = real_core
    _run_cli(["mcp", "setup", "--profile", "all", "--organize"])
    token = profile_mod.read_token(config_dir, "all")
    capsys.readouterr()
    _run_cli(["mcp", "attach", "--profile", "all", "on"])
    assert "выдано" in capsys.readouterr().out
    assert profile_mod.read_meta(config_dir, "all")["scopes"] == ["read", "draft", "organize", "attach"]
    assert profile_mod.read_token(config_dir, "all") == token
    assert mcp_server.profile_has_attach(config_dir, "all")
    _run_cli(["mcp", "attach", "--profile", "all", "off"])
    assert profile_mod.read_meta(config_dir, "all")["scopes"] == ["read", "draft", "organize"]

    _run_cli(["mcp", "setup", "--profile", "sales", "--attach"])
    assert profile_mod.read_meta(config_dir, "sales")["scopes"] == ["read", "draft", "attach"]
