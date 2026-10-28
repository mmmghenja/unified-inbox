"""Ф91: `GET /images/enabled`, `POST /images` — доступ (кука интерфейса или bearer с `attach`), проверки тела,
нет ключа, пределы в сутки по аудиту (Р4), ошибки провайдера понятным текстом без ключа, ответ base64.
Ядро — настоящее приложение в процессе; DashScope — подделка на `httpx.MockTransport`."""

import base64
import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import images as images_mod
from hub.core import security
from hub.core.app_public import create_public_app
from hub.images import config as image_config

from .conftest import KEY, png_bytes

PORT = 8787
BASE_URL = f"http://127.0.0.1:{PORT}"
ORIGIN = BASE_URL


class FakeClock:
    def __init__(self, start_s: float = 1_700_000_000.0) -> None:
        self.value = start_s

    def __call__(self) -> float:
        return self.value

    def advance_ms(self, ms: int) -> None:
        self.value += ms / 1000


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def app(db, clock, config_dir, fake):
    application = create_public_app(db, clock=clock, port=PORT)
    application.state.config_dir = config_dir
    application.state.image_transport = fake.transport
    return application


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as c:
        yield c


def _project(db, name="P") -> int:
    return db.write_sync(
        lambda conn: conn.execute("INSERT INTO main.project (name) VALUES (?)", (name,)).lastrowid
    )


def _bearer(db, scopes=("read", "draft", "attach"), project_ids=None) -> str:
    return db.write_sync(
        lambda conn: security.create_api_token(
            conn,
            name="mcp:t",
            kind="interactive",
            scopes=list(scopes),
            project_ids=project_ids,
            expires_ts=None,
        )
    ).token


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _audit(db, result=None) -> list[tuple[str, dict]]:
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT result, detail_json FROM main.audit WHERE action = 'image' ORDER BY id"
        ).fetchall()
    return [(r, json.loads(d)) for r, d in rows if result is None or r == result]


@pytest.fixture
def keyed(config_dir):
    image_config.save(config_dir, api_key=KEY)
    return config_dir


def _limits(config_dir, project: int, total: int) -> None:
    image_config.config_path(config_dir).write_text(
        json.dumps({"image_daily": {"project": project, "total": total}}), encoding="utf-8"
    )


async def test_enabled(client, db, config_dir):
    token = _bearer(db)
    r = await client.get("/api/v1/images/enabled", headers=_auth(token))
    assert r.status_code == 200 and r.json() == {"enabled": False}
    image_config.save(config_dir, api_key=KEY)
    r = await client.get("/api/v1/images/enabled", headers=_auth(token))
    assert r.json() == {"enabled": True, "daily": {"project": 60, "total": 200}}


async def test_bearer_without_attach_403(client, db, keyed, fake):
    token = _bearer(db, scopes=("read", "draft"))
    r = await client.post("/api/v1/images", json={"prompt": "кот"}, headers=_auth(token))
    assert r.status_code == 403
    assert not fake.requests


async def test_no_key_503(client, db, fake):
    r = await client.post("/api/v1/images", json={"prompt": "кот"}, headers=_auth(_bearer(db)))
    assert r.status_code == 503 and "hub auth alibaba" in r.json()["detail"]
    assert not fake.requests


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        ({"prompt": "  "}, "prompt"),
        ({"prompt": "x" * 2001}, "prompt"),
        ({"prompt": "кот", "n": 5}, "n: от 1 до 4"),
        ({"prompt": "кот", "n": 0}, "n: от 1 до 4"),
        ({"prompt": "кот", "size": "2:1"}, "size"),
        ({"prompt": "кот", "model": "wanx"}, "model"),
        ({"prompt": "кот", "negative": "x" * 501}, "negative"),
    ],
)
async def test_bad_body_422(client, db, keyed, fake, body, needle):
    r = await client.post("/api/v1/images", json=body, headers=_auth(_bearer(db)))
    assert r.status_code == 422 and needle in r.json()["detail"]
    assert not fake.requests


async def test_ok_returns_base64_and_audit(client, db, keyed, fake):
    project = _project(db)
    fake.image = png_bytes(80, 45)
    body = {
        "prompt": "кот на подоконнике",
        "n": 2,
        "size": "1:1",
        "project_id": project,
        "role": "маркетолог",
    }
    r = await client.post("/api/v1/images", json=body, headers=_auth(_bearer(db)))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["model"] == "qwen-image-2.0-pro" and data["n"] == 2 and data["cost_usd"] == 0.15
    assert data["left"] == {"project": 58, "total": 198}
    assert [base64.b64decode(i["data"]) for i in data["images"]] == [fake.image, fake.image]
    assert {(i["mime"], i["width"], i["height"]) for i in data["images"]} == {("image/png", 80, 45)}
    assert fake.gen_bodies[0]["parameters"]["size"] == "2048*2048"
    rows = _audit(db)
    assert [r for r, _ in rows] == ["reserved", "ok"]
    ok = rows[1][1]
    assert ok["project_id"] == project and ok["role"] == "маркетолог" and ok["n"] == 2
    assert ok["model"] == "qwen-image-2.0-pro" and ok["cost_usd"] == 0.15
    assert "кот" not in json.dumps(rows, ensure_ascii=False)  # текста промпта в аудите нет


async def test_fast_model(client, db, keyed, fake):
    r = await client.post("/api/v1/images", json={"prompt": "кот", "fast": True}, headers=_auth(_bearer(db)))
    assert r.status_code == 200
    assert r.json()["cost_usd"] == 0.035 and fake.gen_bodies[0]["model"] == "qwen-image-2.0"


async def test_cookie_ui_allowed(client, db, keyed, clock):
    now_ms = int(clock() * 1000)
    code = db.write_sync(lambda conn: security.create_login_code(conn, now_ms=now_ms))
    login = await client.post("/api/v1/auth/session", json={"code": code}, headers={"Origin": ORIGIN})
    csrf = login.json()["csrf"]
    r = await client.post(
        "/api/v1/images", json={"prompt": "кот"}, headers={"Origin": ORIGIN, "X-Hub-CSRF": csrf}
    )
    assert r.status_code == 200, r.text


async def test_foreign_project_404(client, db, keyed, fake):
    mine = _project(db, "A")
    other = _project(db, "B")
    token = _bearer(db, project_ids=[mine])
    r = await client.post("/api/v1/images", json={"prompt": "кот", "project_id": other}, headers=_auth(token))
    assert r.status_code == 404
    assert not fake.requests


async def test_single_project_token_counts_its_project(client, db, keyed):
    mine = _project(db, "A")
    r = await client.post(
        "/api/v1/images", json={"prompt": "кот"}, headers=_auth(_bearer(db, project_ids=[mine]))
    )
    assert r.status_code == 200
    assert _audit(db, "ok")[0][1]["project_id"] == mine


async def test_project_limit_429_and_window(client, db, keyed, fake, clock):
    _limits(keyed, project=3, total=100)
    project = _project(db)
    token = _bearer(db)
    r = await client.post(
        "/api/v1/images", json={"prompt": "a", "n": 2, "project_id": project}, headers=_auth(token)
    )
    assert r.status_code == 200
    r = await client.post(
        "/api/v1/images", json={"prompt": "a", "n": 2, "project_id": project}, headers=_auth(token)
    )
    assert r.status_code == 429 and "предел картинок проекта: 3 в сутки, осталось 1" in r.json()["detail"]
    calls = len(fake.gen_bodies)
    # другой проект — свой счёт
    other = _project(db, "B")
    r = await client.post("/api/v1/images", json={"prompt": "a", "project_id": other}, headers=_auth(token))
    assert r.status_code == 200 and len(fake.gen_bodies) == calls + 1
    # через сутки предел снова свободен
    clock.advance_ms(images_mod.WINDOW_MS + 1)
    r = await client.post(
        "/api/v1/images", json={"prompt": "a", "n": 3, "project_id": project}, headers=_auth(token)
    )
    assert r.status_code == 200


async def test_total_limit_429(client, db, keyed):
    _limits(keyed, project=50, total=2)
    token = _bearer(db)
    a, b = _project(db, "A"), _project(db, "B")
    first = await client.post(
        "/api/v1/images", json={"prompt": "x", "n": 2, "project_id": a}, headers=_auth(token)
    )
    assert first.status_code == 200
    r = await client.post("/api/v1/images", json={"prompt": "x", "project_id": b}, headers=_auth(token))
    assert r.status_code == 429 and "общий предел картинок хаба: 2 в сутки, осталось 0" in r.json()["detail"]


async def test_failed_call_refunds_limit_and_text_without_key(client, db, keyed, fake):
    _limits(keyed, project=1, total=10)
    project = _project(db)
    token = _bearer(db)
    fake.gen_status = 401
    fake.gen_body = {"code": "InvalidApiKey", "message": f"bad key {KEY}"}
    r = await client.post("/api/v1/images", json={"prompt": "x", "project_id": project}, headers=_auth(token))
    assert r.status_code == 502
    assert "ключ Alibaba не принят" in r.json()["detail"] and KEY not in r.text
    assert [res for res, _ in _audit(db)] == ["reserved", "failed"]
    fake.gen_status, fake.gen_body = 200, None
    r = await client.post("/api/v1/images", json={"prompt": "x", "project_id": project}, headers=_auth(token))
    assert r.status_code == 200  # неудачный вызов предел не съел


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (429, {"code": "Throttling.RateQuota", "message": "slow down"}, 429),
        (400, {"code": "DataInspectionFailed", "message": "inappropriate"}, 422),
        (400, {"code": "Arrearage", "message": "arrears"}, 502),
        (503, {"code": "ServiceUnavailable", "message": "x"}, 502),
    ],
)
async def test_provider_errors_mapped(client, db, keyed, fake, status, body, expected):
    fake.gen_status, fake.gen_body = status, body
    r = await client.post("/api/v1/images", json={"prompt": "x"}, headers=_auth(_bearer(db)))
    assert r.status_code == expected
    assert r.json()["detail"]


async def test_provider_timeout_504(client, db, keyed, fake):
    import httpx

    fake.raise_on_gen = httpx.ReadTimeout("долго")
    r = await client.post("/api/v1/images", json={"prompt": "x"}, headers=_auth(_bearer(db)))
    assert r.status_code == 504 and "не ответил" in r.json()["detail"]


async def test_log_has_no_prompt_or_key(client, db, keyed, caplog):
    caplog.set_level("INFO", logger="hub.core.routes.images")
    r = await client.post("/api/v1/images", json={"prompt": "секретная сцена"}, headers=_auth(_bearer(db)))
    assert r.status_code == 200
    text = caplog.text
    assert "картинки: проект 0, n=1" in text
    assert "секретная" not in text and KEY not in text


async def test_title_overlay_applied(client, db, keyed, fake):
    fake.image = png_bytes(320, 180)
    body = {
        "prompt": "офис",
        "title": "Как подписать договор онлайн",
        "subtitle": "Инструкция",
        "watermark": "umbrella.example.com",
    }
    r = await client.post("/api/v1/images", json=body, headers=_auth(_bearer(db)))
    assert r.status_code == 200, r.text
    item = r.json()["images"][0]
    data = base64.b64decode(item["data"])
    assert data != fake.image and data.startswith(b"\x89PNG")
    assert (item["mime"], item["width"], item["height"]) == ("image/png", 320, 180)
    assert "overlay_failed" not in r.json()
    ok = _audit(db, "ok")[0][1]
    assert ok["title"] is True and ok["watermark"] == "corner"


async def test_nothing_set_image_as_is(client, db, keyed, fake):
    body = {"prompt": "офис", "subtitle": "без заголовка", "watermark_style": "tile"}
    r = await client.post("/api/v1/images", json=body, headers=_auth(_bearer(db)))
    assert base64.b64decode(r.json()["images"][0]["data"]) == fake.image


@pytest.mark.parametrize("style", ["corner", "diagonal", "tile"])
async def test_watermark_styles(client, db, keyed, fake, style):
    fake.image = png_bytes(320, 180)
    body = {"prompt": "офис", "watermark": "umbrella.example.com", "watermark_style": style, "watermark_opacity": 0.5}
    r = await client.post("/api/v1/images", json=body, headers=_auth(_bearer(db)))
    assert r.status_code == 200, r.text
    data = base64.b64decode(r.json()["images"][0]["data"])
    assert data != fake.image and data.startswith(b"\x89PNG")


@pytest.mark.parametrize(
    ("extra", "needle"),
    [
        ({"watermark": "x", "watermark_style": "spiral"}, "watermark_style"),
        ({"watermark": "x", "watermark_opacity": 0.01}, "watermark_opacity"),
        ({"watermark": "x", "watermark_opacity": 0.95}, "watermark_opacity"),
        ({"watermark": "я" * 41}, "watermark"),
    ],
)
async def test_watermark_bad_params_422(client, db, keyed, fake, extra, needle):
    r = await client.post("/api/v1/images", json={"prompt": "x", **extra}, headers=_auth(_bearer(db)))
    assert r.status_code == 422 and needle in r.json()["detail"]
    assert not fake.requests


async def test_title_too_long_422(client, db, keyed, fake):
    r = await client.post(
        "/api/v1/images", json={"prompt": "x", "title": "я" * 121}, headers=_auth(_bearer(db))
    )
    assert r.status_code == 422 and "title" in r.json()["detail"]
    assert not fake.requests


async def test_overlay_failure_returns_paid_image(client, db, keyed, fake, monkeypatch):
    from hub.images import overlay

    def boom(*_a, **_k):
        raise overlay.OverlayError("x")

    monkeypatch.setattr(overlay, "render", boom)
    r = await client.post(
        "/api/v1/images", json={"prompt": "x", "title": "Заголовок"}, headers=_auth(_bearer(db))
    )
    assert r.status_code == 200
    assert r.json()["overlay_failed"] is True
    assert base64.b64decode(r.json()["images"][0]["data"]) == fake.image


async def test_paid_generation_failed_download_spends_limit(client, db, keyed, fake):
    """Ревью 24.10 (1): Alibaba ответил 2xx со ссылками — деньги списаны; скачивание упало — предел всё равно
    потрачен, иначе повторы платят без потолка."""
    _limits(keyed, project=2, total=2)
    project = _project(db)
    token = _bearer(db)
    body = {"prompt": "x", "project_id": project}
    fake.image_status = 500
    for _ in range(2):
        r = await client.post("/api/v1/images", json=body, headers=_auth(token))
        assert r.status_code == 502
        assert "не скачались 1 из 1 (оплачены)" in r.json()["detail"]
    r = await client.post("/api/v1/images", json=body, headers=_auth(token))
    assert r.status_code == 429
    assert len(fake.gen_bodies) == 2  # третьего оплаченного вызова не было
    oks = _audit(db, "ok")
    assert [d["n"] for _, d in oks] == [1, 1]
    assert all(d["missed"] == 1 and d["cost_usd"] > 0 for _, d in oks)
    assert _audit(db, "failed") == []


async def test_partial_download_returns_downloaded(client, db, keyed, fake):
    """Скачалась одна из двух оплаченных: отдаётся она, с пометкой «не скачались 1»; потрачено 2."""
    _limits(keyed, project=10, total=10)
    project = _project(db)
    token = _bearer(db)
    fake.image_fail = {1}
    r = await client.post(
        "/api/v1/images", json={"prompt": "x", "n": 2, "project_id": project}, headers=_auth(token)
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["images"]) == 1 and data["n"] == 1 and data["missed"] == 1
    assert "не скачались 1 из 2 (оплачены)" in data["note"]
    assert data["cost_usd"] == 0.15 and data["left"] == {"project": 8, "total": 8}
    assert [(res, d["n"]) for res, d in _audit(db)] == [("reserved", 2), ("ok", 2)]


async def test_window_edge_failed_does_not_outlive_reserve(client, db, keyed, fake, clock):
    """Ревью 24.10 (2): `failed` пишется позже своего резерва. Резерв вышел из окна 24 ч, а его `failed` ещё в
    окне — он не должен вычитаться из чужих резервов: удачных за сутки не больше предела."""
    _limits(keyed, project=2, total=2)
    project = _project(db)
    token = _bearer(db)
    body = {"prompt": "x", "project_id": project}
    fake.clock = clock
    fake.gen_status, fake.gen_body = 503, {"code": "ServiceUnavailable", "message": "x"}
    fake.gen_advance_s = 60
    assert (await client.post("/api/v1/images", json=body, headers=_auth(token))).status_code == 502
    fake.gen_status, fake.gen_body, fake.gen_advance_s = 200, None, 0
    t_ok = clock.value
    assert (await client.post("/api/v1/images", json=body, headers=_auth(token))).status_code == 200
    clock.value = t_ok - 60 + 24 * 3600 + 30  # резерв неудачного вышел из окна, его `failed` и удачный — нет
    oks = 0
    for _ in range(2):
        r = await client.post("/api/v1/images", json=body, headers=_auth(token))
        oks += r.status_code == 200
    assert oks == 1  # в пределе 2 одно место: удачный ещё в окне
