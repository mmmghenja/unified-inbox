"""Ф41-4б (docs/architecture.md): сжатие разговора в `OcProcess` — `summarize`
моделью сессии, конец по событию `session.compacted`. Сервер — ПОДДЕЛЬНЫЙ (`fake_opencode.py`)."""

from .test_oc_process import harness  # noqa: F401 — фикстура


async def test_compact_between_turns_emits_boundary_and_zero_turn_result(fake_oc, harness):  # noqa: F811
    """`compact()` — `summarize`; конец — `compact_boundary` и `result` с `num_turns: 0`. В ходу — отказ."""
    fake_oc.scenario(turns=[[{"sleep": 0.3}, {"text": "готово"}]])
    h = harness()
    sid = await h.process.start()
    assert await h.say("начни")
    assert await h.process.compact() is False  # ход идёт
    await h.until(lambda: h.of("result"), what="result хода")
    assert fake_oc.jsonl("summarize.jsonl") == []
    assert await h.process.compact() is True
    await h.until(lambda: len(h.of("result")) == 2, what="конец сжатия")
    [call] = fake_oc.jsonl("summarize.jsonl")
    assert call == {"session": sid, "body": {"providerID": "deepseek", "modelID": "deepseek-flash"}}
    [boundary] = h.of("system", "compact_boundary")
    done = h.of("result")[1]
    assert h.messages.index(boundary) < h.messages.index(done)
    assert done["num_turns"] == 0 and done["is_error"] is False and done["engine"] == "opencode"


async def test_compact_refused_by_server_is_error_result_without_boundary(fake_oc, harness):  # noqa: F811
    fake_oc.scenario(turns=[], compact="fail")
    h = harness()
    await h.process.start()
    assert await h.process.compact() is True  # запрос ушёл отдельной задачей
    await h.until(lambda: h.of("result"), what="отказ сжатия")
    [done] = h.of("result")
    assert done["is_error"] is True and done["num_turns"] == 0
    assert h.of("system", "compact_boundary") == []
    assert await h.process.compact() is True  # следующая попытка возможна
