"""Ф41-1: срок кэша и контекст по стенограмме Claude Code."""

import json
import os

from hub.core import transcript

U1 = "11111111-2222-3333-4444-555555555555"
U2 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _asst(ts, *, inp=1, read=0, create=0, h1=0, m5=0, model="claude-x", side=False):
    rec = {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "model": model,
            "usage": {
                "input_tokens": inp,
                "cache_read_input_tokens": read,
                "cache_creation_input_tokens": create,
                "cache_creation": {"ephemeral_1h_input_tokens": h1, "ephemeral_5m_input_tokens": m5},
            },
        },
    }
    if side:
        rec["isSidechain"] = True
    return json.dumps(rec)


def _write(root, session, lines, slug="p1"):
    d = root / "projects" / slug
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{session}.jsonl"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


def test_find_uuid_only_and_freshest(tmp_path):
    assert transcript.find("../../etc/passwd", tmp_path) is None
    assert transcript.find("ses_abc", tmp_path) is None
    old = _write(tmp_path, U1, [], slug="a")
    new = _write(tmp_path, U1, [], slug="b")
    os.utime(old, (1, 1))
    assert transcript.find(U1, tmp_path) == new
    assert transcript.find(U2, tmp_path) is None


def test_basic_state_1h(tmp_path):
    f = _write(tmp_path, U1, [_asst("2026-10-08T10:00:00.000Z", inp=3, read=100, create=7, h1=7)])
    st = transcript.cache_state(f)
    assert st["ttl_s"] == 3600 and st["context_tokens"] == 110 and st["model"] == "claude-x"
    assert st["expires_ts"] == st["last_ts"] + 3_600_000


def test_ttl_branches(tmp_path):
    f = _write(tmp_path, U1, [_asst("2026-10-08T10:00:00Z", create=5, m5=5)])
    assert transcript.cache_state(f)["ttl_s"] == 300
    # чистое чтение: смотрим предыдущую запись с ненулевой разбивкой
    first = _asst("2026-10-08T10:00:00Z", create=5, m5=5)
    f = _write(tmp_path, U1, [first, _asst("2026-10-08T10:01:00Z", read=9)])
    st = transcript.cache_state(f)
    assert st["ttl_s"] == 300 and st["context_tokens"] == 10
    # нигде нет разбивки — 3600
    f = _write(tmp_path, U1, [_asst("2026-10-08T10:01:00Z", read=9)])
    assert transcript.cache_state(f)["ttl_s"] == 3600


def test_skips_synthetic_sidechain_broken(tmp_path):
    f = _write(
        tmp_path,
        U1,
        [
            _asst("2026-10-08T10:00:00Z", read=50, h1=1),
            _asst("2026-10-08T10:05:00Z", model="<synthetic>"),
            _asst("2026-10-08T10:06:00Z", read=999, side=True),
            "{не json",
            json.dumps({"type": "assistant", "timestamp": "x", "message": {"usage": {"input_tokens": "a"}}}),
        ],
    )
    st = transcript.cache_state(f)
    assert st["context_tokens"] == 51


def test_empty_and_no_entries(tmp_path):
    assert transcript.cache_state(_write(tmp_path, U1, [])) is None
    assert transcript.cache_state(_write(tmp_path, U1, [json.dumps({"type": "user"})])) is None


def test_tail_drops_partial_first_line(tmp_path, monkeypatch):
    monkeypatch.setattr(transcript, "TAIL_BYTES", 600)
    pad = "x" * 800
    line = _asst("2026-10-08T10:00:00Z", read=5, h1=1)
    f = _write(tmp_path, U1, [json.dumps({"type": "user", "text": pad}), line])
    assert transcript.cache_state(f)["context_tokens"] == 6
    # запись ассистента обрезана началом хвоста — пропускается, не исключение
    f = _write(tmp_path, U1, [_asst("2026-10-08T10:00:00Z", read=5, h1=1) + " " * 600])
    assert transcript.cache_state(f) is None


def test_state_for_caches_by_mtime(tmp_path, monkeypatch):
    transcript._STATE.clear()
    transcript._PATHS.clear()
    f = _write(tmp_path, U1, [_asst("2026-10-08T10:00:00Z", read=5, h1=1)])
    calls = []
    real = transcript.cache_state
    monkeypatch.setattr(transcript, "cache_state", lambda p: calls.append(p) or real(p))
    assert transcript.state_for(U1, tmp_path)["context_tokens"] == 6
    assert transcript.state_for(U1, tmp_path)["context_tokens"] == 6
    assert len(calls) == 1
    f.write_text(_asst("2026-10-08T10:10:00Z", read=50, h1=1) + "\n")
    assert transcript.state_for(U1, tmp_path)["context_tokens"] == 51
    assert len(calls) == 2
    assert transcript.state_for("nope", tmp_path) is None


def test_cache_bounded(tmp_path):
    transcript._STATE.clear()
    for i in range(transcript.CACHE_LIMIT + 10):
        transcript._STATE[(str(i), i, i)] = None
        transcript._trim(transcript._STATE)
    assert len(transcript._STATE) == transcript.CACHE_LIMIT
