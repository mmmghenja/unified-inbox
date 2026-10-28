"""Ф99-7.3 (П12): хвост стенограммы Claude Code в форме событий ленты."""

import json

from hub.core import transcript

U1 = "11111111-2222-3333-4444-555555555555"
SECRET = "API_KEY=sk-abcdefghijklmnop1234567890"
ASSISTANT_TEXT = [{"type": "thinking", "thinking": "скрыто"}, {"type": "text", "text": "ответ " + SECRET}]
TOOL_USE = [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls " + SECRET}}]


def _line(kind, content, ts="2026-10-28T10:00:00Z", **extra):
    return json.dumps({"type": kind, "timestamp": ts, "message": {"content": content, "model": "m"}, **extra})


def _write(tmp_path, lines):
    d = tmp_path / "projects" / "p"
    d.mkdir(parents=True)
    f = d / f"{U1}.jsonl"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


def test_kinds_masking_and_service_records(tmp_path):
    f = _write(
        tmp_path,
        [
            _line("user", "привет " + SECRET),
            _line("user", "<system-reminder>служебное</system-reminder>"),
            _line("user", "хвост", isSidechain=True),
            "не json",
            _line("assistant", ASSISTANT_TEXT),
            _line("assistant", TOOL_USE),
            _line("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "ok", "is_error": True}]),
        ],
    )
    ev = transcript.tail_events(f, 50)
    assert [e["kind"] for e in ev] == ["say", "text", "tool", "tool_done"]
    assert "sk-abcdefghijklmnop1234567890" not in json.dumps(ev)
    assert ev[0]["summary"].startswith("владелец (next): привет")
    assert ev[2]["tool"] == "Bash" and ev[2]["ref"] == "t1" and ev[3]["ref"] == "t1"
    assert ev[3]["is_error"] is True
    assert len({e["id"] for e in ev}) == 4


def test_last_n_and_reads_from_end(tmp_path):
    lines = [_line("assistant", [{"type": "text", "text": f"m{i}"}]) for i in range(2000)]
    f = _write(tmp_path, lines)
    ev = transcript.tail_events(f, 3)
    assert [e["summary"] for e in ev] == ["m1997", "m1998", "m1999"]
    assert len(transcript.tail_events(f, 10_000)) == transcript.TAIL_MAX
    assert transcript.tail_for(U1, 2, tmp_path)[-1]["summary"] == "m1999"
    assert transcript.tail_for("не-uuid", 2, tmp_path) == []
