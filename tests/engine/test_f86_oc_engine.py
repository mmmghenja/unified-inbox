"""Ф86-1 (docs/architecture.md): OpenCode — справка хаба синтетическими частями
подачи, граница сжатия, которое сервер сделал сам, и признак сервера хаба в окружении (плагин хаба в нём
сводку при сжатии не подкладывает). Сервер — ПОДДЕЛЬНЫЙ (`fake_opencode.py`)."""

from hub.engine.oc_server import server_env

from .test_oc_process import harness  # noqa: F401 — фикстура


def test_server_env_marks_hub_server(tmp_path):
    env = server_env({"PATH": "/bin", "HUB_OC_SERVER": "0"}, config_path=tmp_path / "c.json", password="p")
    assert env["HUB_OC_SERVER"] == "1"


async def test_synthetic_text_blocks_go_as_separate_parts(fake_oc, harness):  # noqa: F811
    fake_oc.scenario(turns=[[{"text": "ок"}], [{"text": "ок"}]])
    h = harness()
    await h.process.start()
    line = {
        "type": "user",
        "session_id": "",
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": "<hub_digest>сводка</hub_digest>", "synthetic": True},
                {"type": "text", "text": "реплика", "synthetic": False},
            ],
        },
        "parent_tool_use_id": None,
        "uuid": "u-1",
    }
    assert await h.process.send(line)
    await h.until(lambda: h.of("result"), what="ход")
    assert await h.say("просто", uid="u-2")
    await h.until(lambda: len(h.of("result")) == 2, what="второй ход")
    calls = fake_oc.jsonl("calls.jsonl")
    prompts = [c["body"]["parts"] for c in calls if c["path"].endswith("/prompt_async")]
    assert prompts[0] == [
        {"type": "text", "text": "<hub_digest>сводка</hub_digest>", "synthetic": True},
        {"type": "text", "text": "реплика"},
    ]
    assert prompts[1] == [{"type": "text", "text": "просто"}]  # без справки — прежняя одна часть


async def test_self_compaction_emits_boundary_without_result(fake_oc, harness):  # noqa: F811
    fake_oc.scenario(turns=[])
    h = harness()
    sid = await h.process.start()
    other = {"type": "session.compacted", "properties": {"sessionID": "ses_other"}}
    await h.process._on_event(other)  # noqa: SLF001
    own = {"type": "session.compacted", "properties": {"sessionID": sid}}
    await h.process._on_event(own)  # noqa: SLF001
    await h.until(lambda: h.of("system", "compact_boundary"), what="граница")  # выдача — по очереди
    [boundary] = h.of("system", "compact_boundary")  # чужая сессия границы не дала
    assert boundary["self_compacted"] is True and boundary["session_id"] == sid
    assert h.of("result") == []
