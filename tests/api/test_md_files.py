"""Просмотр MD-файлов (19.10): `GET /files/md` — только кука интерфейса; путь как его пишут сессии;
относительный — от файла `from`, папки сессии, папки проекта, любого проекта; закрытое хабу не отдаётся."""

import pytest
from httpx import ASGITransport, AsyncClient

from .conftest import BASE_URL, insert_project, login_via_code, make_bearer_token

URL = "/api/v1/files/md"


@pytest.fixture
def home(app, tmp_path):
    home = tmp_path / "home"
    (home / "proj" / "notes").mkdir(parents=True)
    (home / "other").mkdir()
    (home / "hubdata").mkdir()
    (home / "proj" / "notes" / "plan.md").write_text("# План\nсм. [детали](детали.md)", encoding="utf-8")
    (home / "proj" / "notes" / "детали.md").write_text("детали", encoding="utf-8")
    (home / "other" / "ideas.md").write_text("идеи", encoding="utf-8")
    (home / "hubdata" / "x.md").write_text("закрыто", encoding="utf-8")
    (home / "proj" / "code.py").write_text("print()", encoding="utf-8")
    (tmp_path / "outside.md").write_text("снаружи", encoding="utf-8")
    (home / "proj" / "link.md").symlink_to(tmp_path / "outside.md")
    app.state.md_home = home
    app.state.md_closed = [home / "hubdata"]
    return home


@pytest.fixture
async def ui(client, db, clock):
    await login_via_code(client, db, clock)
    return client


async def test_paths_as_sessions_write_them(ui, db, home):
    pid = insert_project(db, name="P", workdir=str(home / "proj"))
    insert_project(db, name="O", workdir=str(home / "other"))
    for raw in (
        str(home / "proj" / "notes" / "plan.md"),
        "~/proj/notes/plan.md",
        f"`file://{home}/proj/notes/plan.md:12`",
        "notes/plan.md",
    ):
        r = await ui.get(URL, params={"path": raw, "project_id": pid})
        assert r.status_code == 200, (raw, r.text)
        assert r.json()["text"].startswith("# План") and r.json()["path"] == str(home / "proj/notes/plan.md")
    # ссылка внутри открытого файла — от его папки; относительный без проекта — от любого проекта
    r = await ui.get(URL, params={"path": "детали.md", "from": str(home / "proj/notes/plan.md")})
    assert r.json()["text"] == "детали"
    r = await ui.get(URL, params={"path": "ideas.md"})
    assert r.json()["text"] == "идеи"


async def test_refusals(ui, db, home):
    insert_project(db, name="P", workdir=str(home / "proj"))
    for raw in ("~/hubdata/x.md", "~/proj/code.py", "~/proj/link.md", "~/proj/нет.md", "../outside.md"):
        r = await ui.get(URL, params={"path": raw})
        assert r.status_code == 404, raw
        assert r.json()["detail"]["code"] == "md_not_found"


async def test_bearer_forbidden(app, db, home):
    token = make_bearer_token(db, scopes=("read",), name="t")
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as c:
        r = await c.get(
            URL, params={"path": "~/other/ideas.md"}, headers={"Authorization": f"Bearer {token}"}
        )
    assert r.status_code == 403
