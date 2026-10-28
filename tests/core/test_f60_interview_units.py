"""Ф60-1, часть A: части подачи (П4), строка ленты подачи (П2) и кадры `event: interview` потока
`/sessions/live` (2.5) — без приложения."""

import asyncio
import json

from hub.core import interview, live

LINE = {
    "id": 1,
    "interview_id": 7,
    "who": "them",
    "start_ms": 1_700_000_000_000,
    "end_ms": 0,
    "created_ts": 0,
}


def test_one_line_longer_than_a_part_is_cut_by_characters():
    text = "я" * 40_000
    parts = interview.build_parts(7, [{**LINE, "text": text}], interview.SERVICE_END)
    assert len(parts) == 3 and all(len(p) <= interview.PART_MAX for p in parts)
    assert parts[-1].splitlines()[0] == interview.SERVICE_END
    assert parts[0].splitlines()[0] == interview.PART_HEAD.format(id=7, k=1, n=3)
    body = "".join(p.splitlines()[2] for p in parts)  # строка блока в каждой части — одна
    assert body.endswith(text) and interview.count_feed_lines("\n".join(parts)) == 1


def test_no_lines_is_a_single_service_line():
    assert interview.build_parts(7, [], interview.SERVICE_HINT_EMPTY) == [interview.SERVICE_HINT_EMPTY]


def test_feed_summary_has_no_text_and_agrees_in_number():
    assert interview.feed_summary(3, 1) == "Интервью #3: подана 1 строка"
    assert interview.feed_summary(3, 3) == "Интервью #3: поданы 3 строки"
    assert interview.feed_summary(3, 11) == "Интервью #3: подано 11 строк"
    assert interview.feed_summary(3, 0) == "Интервью #3: просьба без новых строк"


def test_line_text_is_one_line():
    assert interview.clean_text(" раз\nдва\r\n  три ") == "раз два три"
    assert len(interview.clean_text("x" * 20_000)) == interview.LINE_TEXT_MAX


async def _next(gen) -> str:
    return await asyncio.wait_for(gen.__anext__(), timeout=2.0)


async def test_interview_frames_are_not_collapsed_and_overflow_resyncs():
    hub = live.LiveHub()
    gen = live.stream(hub, now_ms=lambda: 0, heartbeat_s=30)
    waiting = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.05)
    hub.publish_interview({"type": "line", "interview_id": 1, "line": {"id": 1}})
    hub.publish_interview({"type": "line", "interview_id": 1, "line": {"id": 2}})
    first = await asyncio.wait_for(waiting, timeout=2)
    second = await _next(gen)
    assert first.startswith("event: interview\ndata: ") and second.startswith("event: interview\n")
    assert [json.loads(f.split("data: ", 1)[1])["line"]["id"] for f in (first, second)] == [1, 2]

    for n in range(live.INTERVIEW_QUEUE_MAX + 5):
        hub.publish_interview({"type": "line", "interview_id": 1, "line": {"id": 100 + n}})
    frame = await _next(gen)
    assert json.loads(frame.split("data: ", 1)[1]) == {"type": "resync"}
    rest = [json.loads((await _next(gen)).split("data: ", 1)[1])["line"]["id"] for _ in range(5)]
    assert rest == [100 + live.INTERVIEW_QUEUE_MAX + n for n in range(5)]
    hub.close()
    await gen.aclose()
