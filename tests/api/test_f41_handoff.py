"""Ф41-7 (docs/architecture.md, 4): смена модели и усилия сессии хаба и
«Новая с передачей дел» — ядро. Помощники и «мир» — из test_hub_sessions.py; хост поддельный."""

from hub.core import handoff, held, held_work, peer_msgs

from . import test_hub_sessions as base
from .test_hub_sessions import ROLE, B, _audits, _h, _host, _hsub, _mk, _now, _sess

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"
PROC = {"proc_pid": 4242, "proc_start": 777_000}
UUID = "11111111-2222-3333-4444-555555555555"


def _rec(db, sid) -> held.HubSession:
    with db.reader() as conn:
        return held.get(conn, sid)


async def _patch(ui, sid, body, *, expect=200) -> dict:  # noqa: F811
    r = await ui.c.patch(f"{API}/sessions/{sid}", json=body, headers=ui.headers)
    assert r.status_code == expect, r.text
    return r.json()


async def _events(ui, sid) -> list[dict]:  # noqa: F811
    return (await ui.get(f"{API}/sessions/{sid}/events")).json()


async def _say(ui, sid, text, files=None) -> int:  # noqa: F811
    body = {"text": text, "priority": "next", **({"files": files} if files else {})}
    r = await ui.post(f"{API}/sessions/{sid}/say", body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _handoff(ui, sid, body=None, *, expect=200) -> dict:  # noqa: F811
    r = await ui.post(f"{API}/sessions/{sid}/handoff", body or {})
    assert r.status_code == expect, r.text
    return r.json()


def _host_events(db, sid, events, now):
    db.write_sync(lambda conn: held_work.add_events(conn, sid, events, now_ms=now))


# --- смена модели и усилия (раздел 1) -----------------------------------------------------------------------


async def test_model_and_effort_patch(client, ui, db, clock, world):  # noqa: F811
    w = world
    sid = (await _mk(ui, w, model="opus", effort="high"))["id"]
    seq0 = _rec(db, sid).seq
    row = (await _patch(ui, sid, {"model": "sonnet"}))["session"]
    assert (row["model"], row["effort"]) == ("sonnet", "high") and _rec(db, sid).seq > seq0
    assert _audits(db, "hub_session_model") == [
        {
            "hub_session_id": sid,
            "old": {"model": "opus", "effort": "high"},
            "new": {"model": "sonnet", "effort": "high"},
        }
    ]
    (event,) = await _events(ui, sid)
    assert event["kind"] == "status" and event["brief"] == {
        "type": "model",
        "model": "sonnet",
        "effort": "high",
    }
    # То же значение — ничего не пишется; `null` — умолчание движка; оба поля разом.
    seq1 = _rec(db, sid).seq
    await _patch(ui, sid, {"model": "sonnet", "effort": "high"})
    assert _rec(db, sid).seq == seq1 and len(_audits(db, "hub_session_model")) == 1
    row = (await _patch(ui, sid, {"model": None, "effort": " low "}))["session"]
    assert (row["model"], row["effort"]) == (None, "low")
    assert (await _events(ui, sid))[-1]["brief"] == {"type": "model", "model": None, "effort": "low"}
    # Вместе с режимом; длинное и управляющие — 422; не интерфейс — 403; закрытая — 409.
    row = (await _patch(ui, sid, {"mode": "auto", "effort": None}))["session"]
    assert (row["mode"], row["effort"]) == ("auto", None)
    await _patch(ui, sid, {"model": "x" * 65}, expect=422)
    await _patch(ui, sid, {"model": "a\nb"}, expect=422)
    await _patch(ui, sid, {}, expect=422)
    r = await client.patch(f"{API}/sessions/{sid}", json={"model": "haiku"}, headers=_h(w["tok"]))
    assert r.status_code == 403
    (host_row,) = [s for s in (await _host(client, w, "sessions"))["items"] if s["id"] == sid]
    assert (host_row["model"], host_row["effort"]) == (None, None)
    await ui.post(f"{API}/sessions/{sid}/close")
    await _patch(ui, sid, {"model": "haiku"}, expect=409)


# --- передача дел (раздел 2) --------------------------------------------------------------------------------


async def test_handoff_closes_the_old_and_starts_the_new_on_the_same_line(
    client, ui, db, clock, world, tmp_path
):  # noqa: F811
    w = world
    want = {"mode": "direct"}
    old = (
        await _mk(ui, w, role=ROLE, model="opus", effort="high", mode="auto", want_sub=want, title="Маша")
    )["id"]
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.hub_session SET autosend_ok = 0 WHERE id = ?", (old,))
    )
    await _hsub(client, w, old, cwd=w["dir3"], mode="direct")
    await _host(client, w, f"sessions/{old}/bind", {"engine_session_id": UUID, "state": "running", **PROC})

    # Идёт ход — 409; не интерфейс — 403; нет такой — 404.
    await _handoff(ui, old, expect=409)
    r = await client.post(f"{API}/sessions/{old}/handoff", json={}, headers=_h(w["tok"]))
    assert r.status_code == 403
    await _handoff(ui, 9999, expect=404)

    # Лента прежней: реплика владельца, шаги, итог, открытая карточка, ждущие реплики с файлом.
    first = await _say(ui, old, "почини оплату")
    now = _now(clock)
    db.write_sync(lambda conn: held_work.say_delivered(conn, first, now_ms=now))
    _host_events(
        db,
        old,
        [
            {"ts": now, "kind": "tool", "tool": "Bash", "summary": "pytest -q"},
            {"ts": now, "kind": "text", "summary": "Нашёл причину: таймаут шлюза. Осталось поправить тест."},
            {
                "ts": now,
                "kind": "status",
                "summary": "сигнал хаба: сообщений агентов 1",
                "brief": {"type": "signal"},
            },
        ],
        now,
    )
    request = await _host(
        client,
        w,
        f"sessions/{old}/requests",
        {"kind": "permission", "tool": "Bash", "input": {"command": "git push"}, "reason": "гард хаба: push"},
    )
    assert request["id"]
    await _host(client, w, f"sessions/{old}/turn", {"state": "idle"})
    uploads = []
    for name in ("план.txt", "тот же план.txt"):  # вторая копия тех же байтов остаётся у прежней сессии
        upload = await ui.c.post(
            f"{API}/sessions/{old}/files", params={"name": name}, content=b"plan of work", headers=ui.headers
        )
        assert upload.status_code == 200, upload.text
        uploads.append(upload.json())
    file_id = (uploads[0].get("file") or uploads[0])["id"]
    await _say(ui, old, "и вот план", files=[file_id])
    await _say(ui, old, "потом отчитайся")
    # Сообщение агента линии, пока прежняя ещё жива, — достанется новой подписке.
    r = await client.post(
        f"{API}/subs", json={"session": _sess(B, cwd=w["dir3"]), "role": "сосед"}, headers=_h(w["tok"])
    )
    assert r.status_code == 200, r.text
    r = await client.post(
        f"{API}/peer-msgs",
        json={"session": _sess(B), "text": "глянь прод", "to": {"project_id": w["p3"], "role": ROLE}},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 201, r.text

    claude_root = tmp_path / "claude"
    (claude_root / "projects" / "slug").mkdir(parents=True)
    transcript_path = claude_root / "projects" / "slug" / f"{UUID}.jsonl"
    transcript_path.write_text("{}\n")
    original = handoff.transcript.find
    handoff.transcript.find = lambda sid, root=None: original(sid, claude_root)
    try:
        body = await _handoff(ui, old, {"engine": "opencode", "model": "deepseek/deepseek-flash"})
    finally:
        handoff.transcript.find = original

    closed, new = body["closed"], body["session"]
    assert closed["id"] == old and closed["close_reason"] == "handoff" and closed["closed_ts"]
    assert closed["handoff_to"] == new["id"]
    assert (new["role"], new["role_no"], new["project_id"], new["title"]) == (ROLE, 1, w["p3"], "Маша")
    assert (new["engine"], new["model"], new["effort"], new["mode"]) == (
        "opencode",
        "deepseek/deepseek-flash",
        None,
        "auto",
    )
    assert new["want_sub"]["mode"] == "direct" and new["autosend_ok"] is False and new["state"] == "asleep"
    rec = _rec(db, new["id"])
    assert (rec.created_by, rec.autosend_ok, rec.engine_session_id) == ("handoff", 0, None)
    assert _audits(db, "hub_session_handoff")[0]["new_session_id"] == new["id"]
    # Прежняя: запросы закрыты, реплики сняты с пометкой, подписка завершена; повторная передача — 409.
    full = (await ui.get(f"{API}/sessions/{old}")).json()
    assert full["session"]["handoff_to"] == new["id"] and full["requests"] == []
    notes = [e["summary"] for e in await _events(ui, old) if e["kind"] == "say"]
    assert notes[-2:] == [handoff.NOTE_SAY_MOVED] * 2
    await _handoff(ui, old, expect=409)

    # Новая: первая реплика — текст передачи дел, затем снятые реплики в прежнем порядке, файл — при своей.
    with db.reader() as conn:
        says = held_work.queued_says(conn, held.get(conn, new["id"]))
    assert [s["text"] for s in says[1:]] == ["и вот план", "потом отчитайся"]
    assert [[f["id"] for f in s["files"]] for s in says] == [[], [file_id], []]
    text = says[0]["text"]
    assert text.startswith(
        f"Передача дел. Ты продолжаешь работу прежней сессии хаба: проект «Umbrella», роль «{ROLE} 1», "
        "движок Claude Code, её разговор закрыт "
    )
    assert f"Полная запись прежнего разговора: {transcript_path} " in text
    lines = text.split("\n")
    start = lines.index("Последние реплики (от старых к новым):")
    assert lines[start + 1].startswith("[владелец ") and lines[start + 1].endswith("] почини оплату")
    assert lines[start + 2].endswith("] Нашёл причину: таймаут шлюза. Осталось поправить тест.")
    assert lines[start + 3 : start + 5] == ["Открытые вопросы на момент передачи:", "- Bash: гард хаба: push"]
    assert "pytest -q" not in text and "сигнал хаба" not in text and "и вот план" not in text
    assert lines[-1].startswith("Коротко подтверди, что принял дела")
    # Файл жив у новой сессии (байты не стёрты закрытием прежней).
    r = await client.get(f"{API}/work/host/session-files/{file_id}", headers=_h(w["host"]))
    assert r.status_code == 200 and r.content == b"plan of work"
    with db.reader() as conn:
        owners = conn.execute(
            "SELECT session_id, purged_ts IS NULL FROM main.held_file ORDER BY rowid"
        ).fetchall()
    assert owners == [(new["id"], 1), (old, 0)]  # копия прежней стёрта закрытием, перенесённый файл жив

    # Ждущее сообщение агента линии достаётся подписке новой сессии.
    sub = (await _hsub(client, w, new["id"], cwd=w["dir3"], mode="direct"))["sub"]
    with db.reader() as conn:
        assert peer_msgs.signal_count(conn, base.subs.get_sub(conn, sub["id"]), now_ms=_now(clock)) == 1
    # Закрытая не передачей `handoff_to` не несёт.
    plain = (await _mk(ui, w))["id"]
    row = (await ui.post(f"{API}/sessions/{plain}/close")).json()["session"]
    assert "handoff_to" not in row


async def test_handoff_keeps_what_is_not_given(client, ui, db, clock, world):  # noqa: F811
    w = world
    old = (await _mk(ui, w, model="opus", effort="high"))["id"]
    body = await _handoff(ui, old, {"mode": "auto"})
    new = body["session"]
    assert (new["engine"], new["model"], new["effort"], new["mode"]) == ("claude", "opus", "high", "auto")
    assert (new["role"], new["want_sub"], new["autosend_ok"]) == (None, None, True)
    with db.reader() as conn:
        (say,) = held_work.queued_says(conn, held.get(conn, new["id"]))
    assert "роль «без роли»" in say["text"] and "Реплик в прежнем разговоре не было." in say["text"]
    assert "Полная запись" not in say["text"] and "Открытые вопросы на момент передачи: нет." in say["text"]
    # Явный `null` — умолчание движка.
    again = (await _handoff(ui, new["id"], {"model": None}))["session"]
    assert (again["model"], again["effort"]) == (None, "high")


def test_tail_limits_order_and_cut(db, clock, world):  # noqa: F811
    """2.2: с конца, пока сумма не превысит 12 000; каждая реплика — не длиннее 1 500 (середина вырезана)."""
    w = world
    now = _now(clock)
    audit = {"actor": "ui", "api_token_id": None, "client_name": None, "request_id": "t"}

    def _make(conn):
        from hub.core import hub_sessions

        rec = hub_sessions.create(conn, project_id=w["p3"], engine="claude", now_ms=now, audit=audit)
        held_work.say(conn, rec.id, text="самое старое", priority="next", now_ms=now, audit=audit)
        events = [
            {"ts": now + i, "kind": "text", "summary": f"итог {i:02d} " + "я" * 1400} for i in range(12)
        ]
        events.insert(5, {"ts": now, "kind": "compact", "summary": "контекст сжат"})
        events.append({"ts": now + 50, "kind": "text", "summary": "НАЧАЛО" + "ж" * 3000 + "КОНЕЦ"})
        held_work.add_events(conn, rec.id, events, now_ms=now)
        return rec.id

    sid = db.write_sync(_make)
    with db.reader() as conn:
        rec = held.get(conn, sid)
        lines, compacts, last = handoff.tail_lines(conn, rec)
        text = handoff.build_text(conn, rec, project_name="П", requests=[], now_ms=now)
    assert compacts == 1 and last == now
    assert sum(len(line) for line in lines) <= handoff.TAIL_MAX and 7 <= len(lines) <= 9
    assert "самое старое" not in "\n".join(lines)  # не поместилось — отброшено с начала
    numbers = [line.split("итог ")[1][:2] for line in lines[:-1]]
    assert numbers == sorted(numbers) and numbers[-1] == "11"
    last_line = lines[-1].split("] ", 1)[1]
    assert len(last_line) == handoff.ITEM_MAX and handoff.CUT_MARK in last_line
    assert last_line.startswith("НАЧАЛО") and last_line.endswith("КОНЕЦ")
    assert "Контекст сжимался 1 раз" in text and len(text) <= held_work.SAY_MAX
    assert handoff.clip_middle("коротко") == "коротко"


async def test_handoff_gives_chan_subs_and_pending_to_the_new_session(client, ui, db, clock, world):  # noqa: F811
    """владелец 21.10: «Новая с передачей дел» теряла подписки на беседы — прежняя закрывалась `ended_by_owner`,
    а её беседы возвращаются новой линии только «сами» кончившиеся (Ф32-1). Теперь причина `handoff`."""
    w = world
    old = (await _mk(ui, w, role=ROLE, want_sub={"mode": "messages"}))["id"]
    sub_id = (await _hsub(client, w, old, mode="messages"))["sub"]["id"]
    r = await client.post(
        f"{API}/chan-subs",
        json={"channel_id": w["ch2"], "session": base._hs(old, cwd=w["dir3"]), "keywords": ["оплата"]},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
    chan_id = r.json()["sub"]["id"]
    clock.advance_ms(base.MIN)
    base._ingest(db, w, [base._ev("team", "301", _now(clock))], clock)
    assert base._pending(db, sub_id) == 1

    new = (await _handoff(ui, old))["session"]
    assert base._sub_rec(db, sub_id).end_reason == "handoff"
    with db.reader() as conn:
        assert base.chan_subs.get(conn, chan_id).end_reason == "handoff"

    got = await _hsub(client, w, new["id"], mode="messages")
    assert [(row["channel_id"], row["keys"]) for row in got["restored_channels"]] == [(w["ch2"], 1)]
    assert got["sub"]["pending"] == 1  # ждущее прежней — новой подписке линии
    # Обычное закрытие по-прежнему насовсем.
    plain = (await _mk(ui, w, role="другая"))["id"]
    plain_sub = (await _hsub(client, w, plain))["sub"]["id"]
    await ui.post(f"{API}/sessions/{plain}/close")
    assert base._sub_rec(db, plain_sub).end_reason == "ended_by_owner"
