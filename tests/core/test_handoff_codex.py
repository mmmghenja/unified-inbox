"""Ф74-5: «Новая с передачей дел» у сессии Codex называет запись нити
(`~/.codex/sessions/…/rollout-…-<id>.jsonl`)."""

from hub.core import handoff


def test_codex_record_found_by_thread_id(tmp_path):
    thread = "019a0b1c-2d3e-7f40-8a51-6b7c8d9e0f12"
    day = tmp_path / "2026" / "10" / "21"
    day.mkdir(parents=True)
    record = day / f"rollout-2026-10-21T10-00-00-{thread}.jsonl"
    record.write_text("{}\n")
    (day / "rollout-2026-10-21T10-00-00-другая.jsonl").write_text("{}\n")
    assert handoff.codex_record(thread, tmp_path) == record
    assert handoff.codex_record("нет-такой", tmp_path) is None
    assert handoff.codex_record("../x", tmp_path) is None
    assert handoff.codex_record("*", tmp_path) is None
    assert handoff.codex_record("", tmp_path) is None
    assert handoff.ENGINE_NAMES["codex"] == "Codex"


def test_cursor_record_found_by_chat_id(tmp_path):
    """Ф74-8: у Cursor передача дел тоже называет запись прежнего разговора."""
    chat = "5b0f3c1e-9a2d-4e71-b0c8-2f6d4a9e1c37"
    folder = tmp_path / "dev-shm-proj-1" / "agent-transcripts" / chat
    folder.mkdir(parents=True)
    record = folder / f"{chat}.jsonl"
    record.write_text("{}\n")
    assert handoff.cursor_record(chat, tmp_path) == record
    assert handoff.cursor_record("нет-такого", tmp_path) is None
    for bad in ("", "../x", "*", "a/b", "..", "[a]"):
        assert handoff.cursor_record(bad, tmp_path) is None, bad
