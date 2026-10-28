"""Ф41-3в для постоянных сессий на OpenCode (docs/architecture.md): картинка — частью
`file` с data:-URL, только если модель принимает картинки; PDF и всё прочее — в `hub-files`. `opencode` —
ПОДДЕЛЬНЫЙ сервер `tests/engine/fake_opencode.py`; настоящий не запускается."""

import base64
import json
import time

from hub import workdir_files

from .oc_helpers import (
    oc,  # noqa: F401 — фикстура `oc`
    started,
)
from .test_held_host import _events, _full, _session, _turn_done
from .test_held_say_files import NOTES, PDF, PNG, listing, say, upload

SECRET = "sk-КЛЮЧ-ПРОВАЙДЕРА-НЕ-ДОЛЖЕН-УТЕЧЬ"
HUB_FILES = workdir_files.DIR_NAME


def providers(image) -> dict:
    capabilities = {} if image is None else {"capabilities": {"input": {"image": image, "pdf": False}}}
    model = {"limit": {"context": 1_000_000}, **capabilities}
    return {"providers": [{"id": "deepseek", "key": SECRET, "models": {"deepseek-flash": model}}]}


async def _said(env, host, oc, sid, files, text):  # noqa: F811 — фикстура
    before = len(oc.prompts())
    await say(env, sid, files, text)
    await env.until(host, lambda: len(oc.prompts()) > before, what="реплика в OpenCode")
    await _turn_done(env, host, sid)
    assert (await _full(env, sid))["says"] == []
    return oc.prompts()[before]["body"]["parts"]


async def test_oc_image_is_a_file_part_and_the_rest_goes_to_hub_files(env, oc, caplog):  # noqa: F811
    oc.scenario([{"text": "вижу"}], [{"text": "одна картинка"}], providers=providers(True))
    host = await started(env)
    sid, _pid, workdir = await _session(env, engine="opencode")
    png = await upload(env, sid, PNG, "снимок.png")
    pdf = await upload(env, sid, PDF, "договор.pdf")
    txt = await upload(env, sid, NOTES, "заметки.txt")
    with caplog.at_level("DEBUG"):
        parts = await _said(env, host, oc, sid, [png, pdf, txt], "что тут?")
    assert parts == [
        {
            "type": "text",
            "text": "что тут?\n\nПриложены файлы (лежат в папке проекта): "
            f"{HUB_FILES}/договор.pdf, {HUB_FILES}/заметки.txt",
        },
        {
            "type": "file",
            "mime": "image/png",
            "filename": "снимок.png",
            "url": "data:image/png;base64," + base64.b64encode(PNG).decode(),
        },
    ]
    assert listing(workdir / HUB_FILES) == [".gitignore", "договор.pdf", "заметки.txt"]
    assert (workdir / HUB_FILES / "договор.pdf").read_bytes() == PDF

    # Сверка «реплика уже в разговоре» (перезапуск хоста): текст с дописанным абзацем — та же реплика.
    runner = host.held.runners[sid]
    same = {
        "id": 1,
        "text": "что тут?",
        "files": [png, pdf, txt],
        "created_ts": int(time.time() * 1000) - 1000,
    }
    assert await host.held._oc_has_say(runner, same)  # noqa: SLF001
    assert not await host.held._oc_has_say(runner, {**same, "text": "что"})  # noqa: SLF001
    assert not await host.held._oc_has_say(runner, {**same, "files": []})  # noqa: SLF001

    # Только картинка и пустой текст — одна часть, пустой текстовой нет.
    only = await upload(env, sid, PNG, "ещё.png")
    parts = await _said(env, host, oc, sid, [only], "")
    assert [p["type"] for p in parts] == ["file"] and parts[0]["filename"] == "ещё.png"

    # Сведения о модели ещё не пришли (фоновая загрузка не кончилась) — хост запрашивает их сам.
    runner.process.model_info = None
    late = await upload(env, sid, PNG, "поздняя.png")
    parts = await _said(env, host, oc, sid, [late], "а эта?")
    assert [p["type"] for p in parts] == ["text", "file"] and runner.process.model_info["image"] is True

    # Способности модели взяты из ответа с ключом провайдера — ключ никуда не попал.
    log = "\n".join(r.getMessage() for r in caplog.records)
    with env.db.reader() as conn:
        dump = "\n".join(conn.iterdump())
    events = json.dumps(await _events(env, sid), ensure_ascii=False)
    for place in (log, dump, events, json.dumps(oc.prompts(), ensure_ascii=False)):
        assert SECRET not in place
    assert base64.b64encode(PNG).decode() not in dump + log


async def test_oc_model_without_image_input_gets_the_image_as_a_file(env, oc):  # noqa: F811
    for n, image in enumerate((False, None)):
        oc.scenario([{"text": "ок"}], providers=providers(image))
        host = await started(env)
        sid, _pid, workdir = await _session(env, engine="opencode")
        png = await upload(env, sid, PNG, "снимок.png")
        parts = await _said(env, host, oc, sid, [png], "глянь")
        assert parts[-1:] == [
            {
                "type": "text",
                "text": f"глянь\n\nПриложены файлы (лежат в папке проекта): {HUB_FILES}/снимок.png",
            }
        ], n
        assert (workdir / HUB_FILES / "снимок.png").read_bytes() == PNG
        await host.held.runners[sid].kill()


async def test_oc_providers_unavailable_means_no_image_blocks(env, oc):  # noqa: F811
    oc.scenario([{"text": "ок"}])  # `GET /config/providers` — 404
    host = await started(env)
    sid, _pid, workdir = await _session(env, engine="opencode")
    png = await upload(env, sid, PNG, "снимок.png")
    parts = await _said(env, host, oc, sid, [png], "глянь")
    assert [p["type"] for p in parts] == ["text"]
    assert (workdir / HUB_FILES / "снимок.png").read_bytes() == PNG
