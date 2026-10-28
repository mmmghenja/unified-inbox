"""Ф54-1 (docs/architecture.md–3 и поправки П1–П13): заявка «нужен человек» — ядро
и ручки.

- создание (multipart), допуски (кука, ответчик, без `draft`, дежурная, оркестратор, без подписки, без роли,
  вне проектов, несколько проектов), повтор по адресу, пределы по линии, проекту и суткам;
- решения владельца и отказ автору; отмена по линии; сроки минутным циклом без запросов к ручкам;
- сигнал исхода автору один раз, только клиенту протокола 8, наследнику линии; срочность у сессии хаба;
- скриншот: приём (не картинка, больше 5 МиБ), раздача (кука — 200, автор — 403), чистка байтов и строк;
- ответчик-агент: видимость и решение по выключателю вида, свой проект; токен `human` — 403 на прочих ручках;
- `/sync` только UI, кадр SSE без текстов автора.

Мир и помощники — из test_hub_sessions.py."""

import asyncio
import json
import re
import sys

import httpx
import pytest

from hub import cli as hub_cli
from hub import paths
from hub.core import feed, queries, runner, security, subs
from hub.core import human_requests as hr

from . import test_hub_sessions as base
from .agent_hands import AGENT_HANDS, AGENT_READS
from .card_helpers import _route
from .test_f41_autopilot import _put
from .test_f41_incoming import _idle, _signals, _wait_host
from .test_hub_sessions import CODER, MIN, ROLE, A, B, _audits, _h, _hs, _hsub, _mk, _now, _sess, _sub, _token

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
JPEG = b"\xff\xd8\xff\xe0" + b"\x01" * 40
SECRET = "СЕКРЕТНЫЙ-ТЕКСТ-АВТОРА"
# Ручки без принципала вовсе — в переборе «токен human -> 403» не участвуют.
OPEN_ROUTES = {"/api/v1/healthz"}


def _form(body: dict, shot=None) -> dict:
    files: dict = {"data": (None, json.dumps(body, ensure_ascii=False))}
    if shot is not None:
        files["shot"] = ("снимок.png", shot, "application/octet-stream")
    return files


async def _create(client, w, who=A, *, token=None, expect=201, shot=None, session=None, **fields) -> dict:
    body = {"kind": "captcha", "need": "Пройти капчу", "session": session or _sess(who), **fields}
    r = await client.post(f"{API}/human-requests", files=_form(body, shot), headers=_h(token or w["tok"], 8))
    assert r.status_code == expect, r.text
    return r.json()


async def _decide(ui, human_id, outcome="done", *, expect=200) -> dict:  # noqa: F811
    r = await ui.post(f"{API}/human-requests/{human_id}/{outcome}")
    assert r.status_code == expect, r.text
    return r.json()


async def _cancel(client, w, human_id, who=A, *, token=None, expect=200) -> dict:
    r = await client.post(
        f"{API}/human-requests/{human_id}/cancel",
        json={"session": _sess(who)},
        headers=_h(token or w["tok"], 8),
    )
    assert r.status_code == expect, r.text
    return r.json()


async def _mine(client, w, who=A, *, token=None, expect=200, human_id=None, **params):
    me = {"pid": who[0], "proc_start": who[1], "session_id": who[2]}
    path = f"{API}/human-requests" + (f"/{human_id}" if human_id is not None else "")
    r = await client.get(path, params={**me, **params}, headers=_h(token or w["tok"], 8))
    assert r.status_code == expect, r.text
    return r.json()


async def _wait(client, w, sub_id, who, proto=8):
    r = await client.post(
        f"{API}/subs/{sub_id}/wait",
        json={"session": _sess(who), "waiter": "0123456789abcdef", "timeout_s": 1},
        headers=_h(w["tok"], proto),
    )
    assert r.status_code == 200, r.text
    return r.json().get("signals") or []


def _row(db, human_id) -> hr.Human | None:
    with db.reader() as conn:
        return hr.get(conn, human_id)


def _stored(db, sha) -> bool:
    return hr.file_path(db.data_dir, sha).is_file()


def _clean(db, clock) -> dict:
    now = _now(clock)
    return db.write_sync(lambda conn: hr.cleanup(conn, now_ms=now))


def _human_token(db, project_ids=None):
    return _token(db, name="human-agent", kind="background", scopes=("human",), project_ids=project_ids)


# --- создание ----------------------------------------------------------------------------------------------


async def test_create_shape_audit_and_repeat_by_url(client, ui, db, clock, world):  # noqa: F811
    w = world
    sub_id = (await _sub(client, w, A, role=ROLE))["sub"]["id"]
    made = await _create(
        client,
        w,
        site="jobs.example",
        url="https://www.jobs.example/login/",
        doing=SECRET,
        need=f"Пройти капчу {SECRET}",
        profile="fl",
        tab="Вход",
        shot=PNG,
    )
    req = made["request"]
    assert made["created"] is True
    assert set(req) == {
        "id", "project_id", "kind", "site", "url", "doing", "need", "profile", "tab", "shot", "expires_ts",
        "status", "decided_ts", "role", "role_no", "created_ts", "seq", "decided_note",
    }  # fmt: skip  # 12а: источника ответа (`decided_by`) сессия не видит; Ф93-2 — причина исхода видна
    assert (req["project_id"], req["kind"], req["status"]) == (w["p3"], "captcha", "pending")
    assert (req["role"], req["role_no"]) == (ROLE, 1)
    assert req["shot"] == {"size": len(PNG), "mime": "image/png"}
    assert req["expires_ts"] == _now(clock) + 30 * MIN
    row = _row(db, req["id"])
    assert (row.author_sub_id, row.token_id, row.author_role_no) == (sub_id, w["tok_id"], 1)
    assert _stored(db, row.shot_sha256)
    # Аудит — без текста автора и адреса страницы.
    (audit,) = _audits(db, "human_create")
    assert audit["human_id"] == req["id"] and audit["shot"] is True
    dumped = json.dumps(_audits(db, "human_create"), ensure_ascii=False)
    assert SECRET not in dumped and "jobs.example" not in dumped

    # Повтор (П7): та же линия, тот же вид и тот же непустой адрес — та же заявка, 200.
    again = await _create(client, w, url="https://www.jobs.example/login/", expect=200)
    assert again["created"] is False and again["request"]["id"] == req["id"]
    # Другой вид по тому же адресу и заявка без адреса — новые.
    other = await _create(client, w, kind="login", url="https://www.jobs.example/login/")
    bare = await _create(client, w)
    assert len({req["id"], other["request"]["id"], bare["request"]["id"]}) == 3
    # `ttl_min=0` — без срока.
    await _cancel(client, w, bare["request"]["id"])
    assert (await _create(client, w, ttl_min=0))["request"]["expires_ts"] is None


async def test_create_form_refusals(client, ui, db, world):  # noqa: F811
    w = world
    await _sub(client, w, A, role=ROLE)
    h = _h(w["tok"], 8)
    body = {"kind": "captcha", "need": "Пройти", "session": _sess(A)}
    # Не multipart, нет части `data`, кривой JSON, лишнее поле, неизвестный вид, пустое `need`, срок.
    r = await client.post(f"{API}/human-requests", json=body, headers=h)
    assert r.status_code == 422, r.text
    r = await client.post(f"{API}/human-requests", files={"shot": ("a.png", PNG)}, headers=h)
    assert r.status_code == 422, r.text
    r = await client.post(f"{API}/human-requests", files={"data": (None, "{не json")}, headers=h)
    assert r.status_code == 422, r.text
    for bad in (
        {**body, "extra": 1},
        {**body, "kind": "robot"},
        {**body, "need": ""},
        {**body, "need": "   "},
        {**body, "ttl_min": 1441},
        {**body, "ttl_min": -1},
        {**body, "need": "я" * (hr.NEED_MAX + 1)},
        {k: v for k, v in body.items() if k != "session"},
    ):
        r = await client.post(f"{API}/human-requests", files=_form(bad), headers=h)
        assert r.status_code == 422, (bad, r.text)
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.human_request").fetchone()[0] == 0


async def test_create_admission(client, ui, db, world):  # noqa: F811
    w = world
    body = {"kind": "captcha", "need": "Пройти", "session": _sess(A)}
    # Без подписки на проект — 409.
    refused = await _create(client, w, expect=409)
    assert "подпишись на проект" in refused["detail"]
    # Подписка без роли — 409 с подсказкой.
    await _sub(client, w, A)
    refused = await _create(client, w, expect=409)
    assert "hub_subscribe(role=" in refused["detail"]
    await _sub(client, w, A, role=ROLE)
    # Кука — 403 (владелец заявку не заводит).
    r = await ui.c.post(f"{API}/human-requests", files=_form(body), headers=ui.headers)
    assert r.status_code == 403, r.text
    # Ответчик-агент, bearer без `draft`, дежурная, оркестратор, фоновый — 403.
    agent, _ = _human_token(db)
    readonly, _ = _token(db, name="mcp:ro", scopes=("read",))
    wake, _ = _token(db, name="mcp:wake-3", scopes=("read", "draft"))
    orch, _ = _token(
        db, name="mcp:orchestrator", scopes=("read", "draft", "organize", "orchestrate")
    )
    back, _ = _token(db, name="svc", kind="background", scopes=("read", "draft"))
    for token in (agent, readonly, wake, orch, back, w["host"]):
        await _create(client, w, token=token, expect=403)
    # Чужой токен с правом, но без своей подписки — 409 (подписка считается под своим токеном).
    other, _ = _token(db, name="mcp:other")
    await _create(client, w, token=other, expect=409)
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.human_request").fetchone()[0] == 0
    assert (await _create(client, w))["request"]["project_id"] == w["p3"]


async def test_create_names_project_when_session_has_several(client, ui, db, world):  # noqa: F811
    w = world
    await _sub(client, w, A, role=ROLE)
    await _sub(client, w, A, cwd=w["dir4"], role=ROLE)
    refused = await _create(client, w, expect=409)
    assert "назови project_id" in refused["detail"] and str(w["p4"]) in refused["detail"]
    assert (await _create(client, w, project_id=w["p4"]))["request"]["project_id"] == w["p4"]
    refused = await _create(client, w, project_id=w["nodir"], expect=409)
    assert "нет подписки на проект" in refused["detail"]


async def test_limits_by_line_project_and_day(client, ui, db, clock, world, monkeypatch):  # noqa: F811
    w = world
    await _sub(client, w, A, role=ROLE)
    await _sub(client, w, B, role=CODER)
    ids = [(await _create(client, w))["request"]["id"] for _ in range(hr.OPEN_PER_LINE)]
    # Предел линии: четвёртая открытая — 429; другая линия проекта — своя квота.
    refused = await _create(client, w, expect=429)
    assert refused["detail"] == hr.LIMIT_LINE
    await _create(client, w, B)
    # Закрытая заявка место освобождает.
    await _decide(ui, ids[0])
    await _create(client, w)
    # Предел проекта.
    monkeypatch.setattr(hr, "OPEN_PER_PROJECT", 5)
    await _create(client, w, B)
    refused = await _create(client, w, B, expect=429)
    assert refused["detail"] == hr.LIMIT_PROJECT
    # Предел за сутки: считаются и закрытые; через сутки — снова можно.
    monkeypatch.setattr(hr, "OPEN_PER_PROJECT", 100)
    monkeypatch.setattr(hr, "NEW_PER_PROJECT_DAY", 6)
    refused = await _create(client, w, B, expect=429)
    assert refused["detail"] == hr.LIMIT_DAY
    clock.advance_ms(24 * 60 * MIN + 1)
    await _create(client, w, B)


# --- решение и отмена --------------------------------------------------------------------------------------


async def test_decide_by_owner_and_refusals(client, ui, db, clock, world):  # noqa: F811
    w = world
    await _sub(client, w, A, role=ROLE)
    first = (await _create(client, w))["request"]["id"]
    second = (await _create(client, w))["request"]["id"]
    third = (await _create(client, w, ttl_min=1))["request"]["id"]
    # Автор своим токеном решить не может — 403 и запись в аудит; прочий bearer — тоже.
    for outcome in hr.OUTCOMES:
        r = await client.post(f"{API}/human-requests/{first}/{outcome}", json={}, headers=_h(w["tok"], 8))
        assert r.status_code == 403, r.text
    assert len(_audits(db, "ui_only_denied")) == 3 and _row(db, first).status == "pending"
    done = await _decide(ui, first, "later")
    assert (done["status"], done["decided_by"], done["decided_ts"]) == ("later", "ui", _now(clock))
    assert _row(db, first).decided_token_id is None
    (audit,) = _audits(db, "human_decide")
    assert audit["human_id"] == first
    # Повторное решение, решение по отменённой и по просроченной — 409 с русским текстом.
    assert "уже закрыта" in (await _decide(ui, first, "done", expect=409))["detail"]
    await _cancel(client, w, second)
    assert "уже закрыта" in (await _decide(ui, second, expect=409))["detail"]
    clock.advance_ms(MIN + 1)
    assert "уже закрыта" in (await _decide(ui, third, "failed", expect=409))["detail"]
    assert _row(db, third).status == "expired"
    await _decide(ui, 999_999, expect=404)
    r = await ui.post(f"{API}/human-requests/{first}/approve")
    assert r.status_code in (404, 405)


async def test_cancel_by_line(client, ui, db, clock, world):  # noqa: F811
    w = world
    sub_id = (await _sub(client, w, A, role=ROLE))["sub"]["id"]
    await _sub(client, w, B, role=CODER)
    req = (await _create(client, w, shot=PNG))["request"]
    sha = _row(db, req["id"]).shot_sha256
    # Кука, ответчик, сессия без подписки с ролью — 403; сессия другой линии — 404.
    r = await ui.post(f"{API}/human-requests/{req['id']}/cancel", {"session": _sess(A)})
    assert r.status_code == 403, r.text
    agent, _ = _human_token(db)
    await _cancel(client, w, req["id"], token=agent, expect=403)
    other, _ = _token(db, name="mcp:other")
    await _cancel(client, w, req["id"], token=other, expect=403)
    await _cancel(client, w, req["id"], B, expect=404)
    assert _row(db, req["id"]).status == "pending"
    # Автор: снята, байты скриншота стёрты сразу, сигнала нет.
    cancelled = await _cancel(client, w, req["id"])
    assert (cancelled["status"], cancelled["shot"]) == ("cancelled", None) and "decided_by" not in cancelled
    assert not _stored(db, sha)
    assert await _wait(client, w, sub_id, A) == []
    assert "уже закрыта" in (await _cancel(client, w, req["id"], expect=409))["detail"]
    # Отмена решённой — 409.
    decided = (await _create(client, w))["request"]["id"]
    await _decide(ui, decided)
    assert "уже закрыта" in (await _cancel(client, w, decided, expect=409))["detail"]


# --- срок (П1) ---------------------------------------------------------------------------------------------


async def test_expiry_by_minute_loop_without_any_request(client, ui, db, clock, world):  # noqa: F811
    w = world
    sub_id = (await _sub(client, w, A, role=ROLE))["sub"]["id"]
    short = (await _create(client, w, ttl_min=1))["request"]["id"]
    long = (await _create(client, w, ttl_min=0))["request"]["id"]
    stop = asyncio.Event()
    ticks: list[float] = []

    async def _sleep(seconds: float) -> None:
        ticks.append(seconds)
        if len(ticks) > 1:
            stop.set()

    # Срок не вышел — цикл ничего не пишет.
    seq_before = _row(db, short).seq
    await runner._human_loop(db, clock, sleep=_sleep, stop_event=stop)
    assert ticks == [60, 60] and (_row(db, short).status, _row(db, short).seq) == ("pending", seq_before)
    # Срок вышел: ни одного запроса к ручкам — заявка снята, сигнал автору готов.
    clock.advance_ms(MIN + 1)
    stop.clear()
    ticks.clear()
    await runner._human_loop(db, clock, sleep=_sleep, stop_event=stop)
    row = _row(db, short)
    assert (row.status, row.decided_ts, row.decided_by) == ("expired", _now(clock), None)
    assert row.seq > seq_before and _row(db, long).status == "pending"
    with db.reader() as conn:
        assert hr.signal_count(conn, subs.get_sub(conn, sub_id), now_ms=_now(clock)) == 1
    assert await _wait(client, w, sub_id, A) == [{"kind": "human", "human_id": short, "outcome": "expired"}]
    # Заявка без срока снимается через сутки — чтением списка (П1: чтение само снимает просроченное).
    clock.advance_ms(24 * 60 * MIN)
    listed = await _mine(client, w, status="expired")
    assert long in [item["id"] for item in listed]


# --- сигнал исхода -----------------------------------------------------------------------------------------


async def test_outcome_signal_once_protocol_8_and_heir_of_the_line(client, ui, db, clock, world):  # noqa: F811
    w = world
    sub_id = (await _sub(client, w, A, role=ROLE))["sub"]["id"]
    first = (await _create(client, w, need=SECRET))["request"]["id"]
    second = (await _create(client, w))["request"]["id"]
    assert await _wait(client, w, sub_id, A) == []  # заявка ждёт — сигнала нет
    await _decide(ui, first, "done")
    await _decide(ui, second, "failed")
    # Клиент протокола 7 вида `human` не знает — ему сигнал не выдаётся и не сгорает.
    assert await _wait(client, w, sub_id, A, proto=7) == []
    assert await _wait(client, w, sub_id, A) == [
        {"kind": "human", "human_id": first, "outcome": "done"},
        {"kind": "human", "human_id": second, "outcome": "failed"},
    ]
    assert await _wait(client, w, sub_id, A) == []  # один раз
    delivered = [a for a in _audits(db, "sub_signal") if a.get("kind") == "human"]
    assert len(delivered) == 2 and SECRET not in json.dumps(delivered, ensure_ascii=False)
    assert _row(db, first).signal_sub_id == sub_id

    # Наследник линии (П8): подписка автора снята, роль подписалась заново другой сессией — исход ей.
    third = (await _create(client, w))["request"]["id"]
    r = await client.post(
        f"{API}/subs/{sub_id}/end",
        json={"session": _sess(A), "reason": "unsubscribed"},
        headers=_h(w["tok"], 8),
    )
    assert r.status_code == 200, r.text
    await _decide(ui, third, "later")
    heir = (await _sub(client, w, B, role=ROLE))["sub"]["id"]
    assert await _wait(client, w, heir, B) == [{"kind": "human", "human_id": third, "outcome": "later"}]
    # Наследник видит заявку линии целиком и в списке.
    assert third in [item["id"] for item in await _mine(client, w, B)]
    # Другая роль проекта исхода не получает.
    fourth = (await _create(client, w, B))["request"]["id"]
    coder = (await _sub(client, w, A, role=CODER))["sub"]["id"]
    await _decide(ui, fourth)
    assert await _wait(client, w, coder, A) == []
    clock.advance_ms(MIN + 1)  # пауза подписки между выдачами (общая для всех видов сигналов)
    assert await _wait(client, w, heir, B) == [{"kind": "human", "human_id": fourth, "outcome": "done"}]


async def test_outcome_is_urgent_for_a_hub_session(client, ui, db, world):  # noqa: F811
    w = world
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    await _hsub(client, w, hub_id)
    req = (await _create(client, w, session=_hs(hub_id)))["request"]["id"]
    assert (await _wait_host(client, w, known={}))["sessions"]["signals"] == []
    await _decide(ui, req)
    assert (await _wait_host(client, w, known={}))["sessions"]["signals"] == [
        {"id": hub_id, "ready": 1, "urgent": 1, "next_ms": None}
    ]
    await _idle(client, w, hub_id)
    assert (await _signals(client, w, hub_id))["signals"] == [
        {"kind": "human", "human_id": req, "outcome": "done"}
    ]
    assert (await _wait_host(client, w, known={}))["sessions"]["signals"] == []


# --- чтение сессией (П8) -----------------------------------------------------------------------------------


async def test_session_reads_only_its_line(client, ui, db, world):  # noqa: F811
    w = world
    await _sub(client, w, A, role=ROLE)
    await _sub(client, w, B, role=CODER)
    mine = (await _create(client, w, need=SECRET))["request"]["id"]
    theirs = (await _create(client, w, B))["request"]["id"]
    assert [item["id"] for item in await _mine(client, w)] == [mine]
    assert (await _mine(client, w, human_id=mine))["need"] == SECRET
    await _mine(client, w, human_id=theirs, expect=404)
    await _mine(client, w, human_id=999_999, expect=404)
    # Без тройки сессии, без подписки с ролью, чужим токеном — 403 (и на несуществующую тоже).
    r = await client.get(f"{API}/human-requests", headers=_h(w["tok"], 8))
    assert r.status_code == 403, r.text
    other, _ = _token(db, name="mcp:other")
    await _mine(client, w, token=other, expect=403)
    await _mine(client, w, token=other, human_id=mine, expect=403)
    await _mine(client, w, token=other, human_id=999_999, expect=403)
    # Владельцу — всё; фильтры `status` и `project_id`.
    listed = (await ui.get(f"{API}/human-requests")).json()
    assert [item["id"] for item in listed] == [theirs, mine]
    await _decide(ui, mine)
    pending = (await ui.get(f"{API}/human-requests", params={"status": "pending"})).json()
    assert [item["id"] for item in pending] == [theirs]
    assert (await ui.get(f"{API}/human-requests", params={"project_id": w["p4"]})).json() == []
    assert (await ui.get(f"{API}/human-requests/{mine}")).json()["status"] == "done"
    assert (await ui.get(f"{API}/human-requests/999999")).status_code == 404


# --- скриншот (П2, П10) ------------------------------------------------------------------------------------


async def test_shot_upload_refusals(client, ui, db, world):  # noqa: F811
    w = world
    await _sub(client, w, A, role=ROLE)
    # Не картинка (по сигнатуре байтов, имя и заявленный тип не важны) и пустой файл — 422.
    for bad in (b"<svg onload=alert(1)>", b"GIF89a" + b"\x00" * 20, b"%PDF-1.7 ...", b""):
        await _create(client, w, shot=bad, expect=422)
    # Больше 5 МиБ — 413, по счёту байт и по `Content-Length`.
    await _create(client, w, shot=PNG + b"\x00" * hr.SHOT_MAX_BYTES, expect=413)
    await _create(client, w, shot=PNG + b"\x00" * (hr.SHOT_MAX_BYTES + 200_000), expect=413)
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.human_request").fetchone()[0] == 0
    root = hr.root(db.data_dir)
    assert not root.exists() or [p for p in root.rglob("*") if p.is_file()] == []
    # Ровно 5 МиБ — принят; JPEG — свой тип.
    exact = PNG + b"\x00" * (hr.SHOT_MAX_BYTES - len(PNG))
    assert (await _create(client, w, shot=exact))["request"]["shot"]["size"] == hr.SHOT_MAX_BYTES
    assert (await _create(client, w, shot=JPEG))["request"]["shot"]["mime"] == "image/jpeg"
    # Отказ после приёма файла (предел линии) временного файла не оставляет.
    await _create(client, w)
    await _create(client, w, shot=PNG, expect=429)
    assert [p.name for p in root.iterdir() if p.name.startswith(hr.TMP_PREFIX)] == []


async def test_shot_serving_and_cleanup(client, ui, db, clock, world):  # noqa: F811
    w = world
    await _sub(client, w, A, role=ROLE)
    with_shot = (await _create(client, w, shot=PNG))["request"]["id"]
    same_bytes = (await _create(client, w, shot=PNG))["request"]["id"]
    without = (await _create(client, w))["request"]["id"]
    sha = _row(db, with_shot).shot_sha256
    r = await ui.get(f"{API}/human-requests/{with_shot}/shot")
    assert r.status_code == 200 and r.content == PNG
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-disposition"] == "inline"
    assert "sandbox" in r.headers["content-security-policy"]
    assert (await ui.get(f"{API}/human-requests/{without}/shot")).status_code == 404
    assert (await ui.get(f"{API}/human-requests/999999/shot")).status_code == 404
    # Автор и прочий bearer — 403.
    other, _ = _token(db, name="mcp:other")
    for token in (w["tok"], other, w["host"]):
        r = await client.get(f"{API}/human-requests/{with_shot}/shot", headers=_h(token, 8))
        assert r.status_code == 403, r.text
    # Один файл на содержимое: отмена одной заявки байтов второй не трогает.
    await _cancel(client, w, same_bytes)
    assert _stored(db, sha)
    # Чистка: ждущую не трогает; через 7 суток после решения байты стёрты, строка жива.
    await _decide(ui, with_shot)
    clock.advance_ms(7 * 24 * 60 * MIN - 1)
    assert _clean(db, clock)["purged"] == 0 and _stored(db, sha)
    clock.advance_ms(2)
    assert _clean(db, clock)["purged"] == 1 and not _stored(db, sha)
    assert (await ui.get(f"{API}/human-requests/{with_shot}/shot")).status_code == 404
    assert (await ui.get(f"{API}/human-requests/{with_shot}")).json()["shot"] is None
    # Через 30 дней закрытые строки удалены.
    clock.advance_ms(23 * 24 * 60 * MIN)
    assert _clean(db, clock)["removed"] == 3
    assert _row(db, with_shot) is None and _row(db, without) is None


# --- ответчик-агент (П3–П5) --------------------------------------------------------------------------------


async def _agent(client, token, method, path, **kw):
    return await client.request(method, f"{API}{path}", headers=_h(token, 8), **kw)


async def test_agent_sees_and_decides_only_under_a_lifted_switch(client, ui, db, clock, world):  # noqa: F811
    w = world
    await _sub(client, w, A, role=ROLE)
    await _sub(client, w, A, cwd=w["dir4"], role=ROLE)
    agent, agent_id = _human_token(db)
    captcha = (await _create(client, w, project_id=w["p3"], shot=PNG, need=SECRET))["request"]["id"]
    login = (await _create(client, w, project_id=w["p3"], kind="login", shot=PNG))["request"]["id"]
    foreign = (await _create(client, w, project_id=w["p4"]))["request"]["id"]

    # Выключатель стоит (умолчание): агент ничего не видит и не решает — 409 «ждёт владельца» на все три исхода.
    assert (await _agent(client, agent, "GET", "/human-requests")).json() == []
    assert (await _agent(client, agent, "GET", f"/human-requests/{captcha}")).status_code == 404
    assert (await _agent(client, agent, "GET", f"/human-requests/{captcha}/shot")).status_code == 404
    for outcome in hr.OUTCOMES:
        r = await _agent(client, agent, "POST", f"/human-requests/{captcha}/{outcome}", json={})
        assert (r.status_code, r.json()["detail"]) == (409, hr.WAITS_OWNER), r.text
    assert _row(db, captcha).status == "pending"

    # Владелец снял выключатель для проекта 3 (один на все виды, 13.10.2026): видны и капча, и вход проекта 3;
    # проект 4 — нет.
    await _route(ui, w, {"human": "agent"})  # Ф66-1: таблица маршрута вместо выключателя `human_agent`
    listed = (await _agent(client, agent, "GET", "/human-requests", params={"status": "pending"})).json()
    assert sorted(item["id"] for item in listed) == sorted([captcha, login])
    assert next(item for item in listed if item["id"] == captcha)["need"] == SECRET
    assert (await _agent(client, agent, "GET", f"/human-requests/{login}")).status_code == 200
    assert (await _agent(client, agent, "GET", f"/human-requests/{captcha}")).json()["tab"] is None
    r = await _agent(client, agent, "GET", f"/human-requests/{captcha}/shot")
    assert r.status_code == 200 and r.content == PNG
    for hidden in (foreign,):
        assert (await _agent(client, agent, "GET", f"/human-requests/{hidden}")).status_code == 404
        assert (await _agent(client, agent, "GET", f"/human-requests/{hidden}/shot")).status_code == 404
        for outcome in hr.OUTCOMES:
            r = await _agent(client, agent, "POST", f"/human-requests/{hidden}/{outcome}", json={})
            assert (r.status_code, r.json()["detail"]) == (409, hr.WAITS_OWNER), r.text

    # Токен ответчика, ограниченный проектом 4: заявка проекта 3 для него не существует.
    narrow, _ = _human_token(db, project_ids=[w["p4"]])
    assert (await _agent(client, narrow, "GET", "/human-requests")).json() == []
    r = await _agent(client, narrow, "POST", f"/human-requests/{captcha}/done", json={})
    assert r.status_code == 404, r.text

    # Решение агента: кто ответил, чей токен, аудит от имени агента.
    r = await _agent(client, agent, "POST", f"/human-requests/{captcha}/done", json={})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "done" and "decided_by" not in r.json()
    assert _row(db, captcha).decided_by == "agent"
    assert _row(db, captcha).decided_token_id == agent_id
    with db.reader() as conn:
        actor, token_id = conn.execute(
            "SELECT actor, api_token_id FROM main.audit WHERE action = 'human_decide'"
        ).fetchone()
    assert (actor, token_id) == ("agent", agent_id)
    r = await _agent(client, agent, "POST", f"/human-requests/{captcha}/failed", json={})
    assert r.status_code == 409 and "уже закрыта" in r.json()["detail"]
    # Владелец поставил выключатель обратно — новые заявки снова только ему.
    await _route(ui, w, {"human": "me"})
    again = (await _create(client, w, project_id=w["p3"]))["request"]["id"]
    r = await _agent(client, agent, "POST", f"/human-requests/{again}/done", json={})
    assert (r.status_code, r.json()["detail"]) == (409, hr.WAITS_OWNER)
    r = await _agent(client, agent, "POST", f"/human-requests/{login}/done", json={})
    assert (r.status_code, r.json()["detail"]) == (409, hr.WAITS_OWNER)  # выключатель стоит — любой вид владельцу
    await _decide(ui, login, "done")


def test_human_scope_only_as_a_separate_token(db):
    for scopes in (("human", "read"), ("human", "draft"), ("human", "read", "draft", "attach")):
        with pytest.raises(ValueError, match="human"):
            _token(db, name="x", scopes=scopes)
    _human_token(db)
    _human_token(db, project_ids=[1])
    # Только фоновый: interactive-токену скоуп `human` не выдаётся.
    with pytest.raises(ValueError, match="background"):
        _token(db, name="human-agent", kind="interactive", scopes=("human",))


def test_attach_is_never_appended_to_a_human_token(db):
    """Ревью Ф54-1: `set_attach` не дописывает `attach` токену ответчика — даже если строка токена в базе
    окажется interactive (старый выпуск), набор остаётся ровно `{human}`."""
    _tok, token_id = _human_token(db)
    for kind in ("background", "interactive"):
        db.write_sync(
            lambda conn, kind=kind: conn.execute(
                "UPDATE main.api_token SET kind = ? WHERE id = ?", (kind, token_id)
            )
        )
        for on in (True, False):
            with pytest.raises(security.AttachError) as err:
                db.write_sync(
                    lambda conn, on=on: security.set_attach(conn, token_id=token_id, on=on, now_ms=1)
                )
            assert err.value.status == 409
        with db.reader() as conn:
            scopes = conn.execute("SELECT scopes FROM main.api_token WHERE id = ?", (token_id,)).fetchone()
        assert scopes[0] == "human"


async def test_human_token_gets_403_on_every_other_route(app, client, db, world):
    """П3: токен ровно со скоупом `human` принимают только ручки заявок — все прочие маршруты ядра отвечают
    ему 403 (или 401/404 там, где bearer не принимается вовсе), 2xx — нигде."""
    agent, _ = _human_token(db)
    allowed = "/api/v1/human-requests"
    checked = 0
    for template, methods in app.openapi()["paths"].items():
        if template.startswith(allowed) or template.startswith("/api/v1/auth/") or template in OPEN_ROUTES:
            continue
        if template.startswith("/api/v1/owner_channel/"):
            continue  # Ф56-1а (П10): очередь канала владельца открыта его агенту (test_f56_queue.py)
        assert template.startswith("/api/v1/"), template
        path = re.sub(r"\{[^}]+\}", "1", template)
        for method in sorted(set(methods) & {"get", "post", "put", "patch", "delete"}):
            if (method, template) in AGENT_HANDS | AGENT_READS:
                continue  # Ф88-1: руки агента над сессиями (test_f88_agent_session_hands.py)
            kw = {} if method == "get" else {"json": {}}
            r = await asyncio.wait_for(client.request(method, path, headers=_h(agent, 8), **kw), timeout=5)
            assert r.status_code in (401, 403), (method, template, r.status_code, r.text[:200])
            checked += 1
    assert checked > 150
    # Свои ручки принимают его (список пуст — выключатели стоят).
    r = await client.get(f"{API}/human-requests", headers=_h(agent, 8))
    assert r.status_code == 200 and r.json() == []


# --- /sync и SSE (П13) -------------------------------------------------------------------------------------


async def test_sync_only_for_ui_and_sse_frame_without_author_text(client, ui, db, world):  # noqa: F811
    w = world
    await _sub(client, w, A, role=ROLE)
    made = await _create(client, w, need=SECRET, site="jobs.example", url="https://jobs.example/x", doing=SECRET)
    req = made["request"]
    sync = (await ui.get(f"{API}/sync", params={"since": 0})).json()
    (item,) = sync["human_requests"]
    assert item["id"] == req["id"] and item["need"] == SECRET and item["seq"] == req["seq"]
    # Bearer (и автор) раздела не получает.
    r = await client.get(f"{API}/sync", params={"since": 0}, headers=_h(w["tok"], 8))
    assert r.status_code == 200 and "human_requests" not in r.json()
    assert SECRET not in r.text
    # Кадр SSE — номера и статус, без текстов автора.
    (frame,) = feed._frames(queries.Changes([], [], req["seq"], False, [], [], human_requests=[item]))
    assert frame.startswith(f"id: {req['seq']}\nevent: human_request\n")
    assert json.loads(frame.split("data: ")[1]) == {
        "id": req["id"],
        "project_id": w["p3"],
        "kind": "captcha",
        "status": "pending",
        "seq": req["seq"],
    }
    assert SECRET not in frame and "jobs.example" not in frame
    # Решение двигает `seq` — заявка приходит в `/sync` снова.
    await _decide(ui, req["id"])
    sync = (await ui.get(f"{API}/sync", params={"since": req["seq"]})).json()
    assert [(i["id"], i["status"]) for i in sync["human_requests"]] == [(req["id"], "done")]


async def test_switch_of_the_kind_is_per_project_and_moves_no_session(client, ui, db, world):  # noqa: F811
    """П6 (Ф66-1: таблица маршрута вместо ключа `human_agent`): маршрут — по проекту заявки, не по сессии:
    запись таблицы не двигает `seq` сессий хаба и не входит в их `auto_free`; ключа `human_agent` в каталоге
    выключателей больше нет (422)."""
    w = world
    sid = (await _mk(ui, w, role=ROLE, mode="auto"))["id"]
    before = (await ui.get(f"{API}/sessions/{sid}")).json()["session"]
    await _route(ui, w, {"human": "agent"})
    after = (await ui.get(f"{API}/sessions/{sid}")).json()["session"]
    assert after["seq"] == before["seq"]
    assert "human_agent" not in (after.get("auto_free") or [])
    assert after.get("auto_held") == before.get("auto_held")
    await _put(ui, "global", "human_agent", False, expect=422)


# --- П3: выпуск токена ответчика ----------------------------------------------------------------------------


def test_cli_human_agent_token(monkeypatch, tmp_path, capsys):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"token": "hubt_secret_value", "token_id": 31})

    monkeypatch.setattr(
        hub_cli,
        "_admin_client",
        lambda *, timeout=None: httpx.Client(
            transport=httpx.MockTransport(handler), base_url="http://hub-internal/internal/v1"
        ),
    )
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    for argv in (["human-agent", "token"], ["human-agent", "token", "--project", "5", "--project", "3"]):
        monkeypatch.setattr(sys, "argv", ["hub", *argv])
        hub_cli.main()
    base_body = {"name": "human-agent", "kind": "background", "scopes": ["human"], "ttl_days": None}
    assert seen == [{**base_body, "project_ids": None}, {**base_body, "project_ids": [3, 5]}]
    captured = capsys.readouterr()
    # Значение — только в stdout (одной строкой); номер и подсказка отзыва — в stderr.
    assert captured.out.splitlines() == ["hubt_secret_value", "hubt_secret_value"]
    assert "hubt_secret_value" not in captured.err and "hub token revoke 31" in captured.err


# --- ревью Ф54-1: смена роли на месте не отрывает заявку от автора -----------------------------------------


async def test_rerole_in_place_keeps_request_with_its_author(client, ui, db, clock, world):  # noqa: F811
    """Второй `hub_subscribe(role=…)` меняет роль в той же строке подписки: заявки остаются у автора —
    видны, снимаются, входят в предел и в повтор по адресу; исход приходит ему же."""
    w = world
    first = await _sub(client, w, A, role=ROLE)
    made = [(await _create(client, w, url=f"https://a/{i}"))["request"]["id"] for i in range(3)]
    second = await _sub(client, w, A, role=CODER)
    assert first["sub"]["id"] == second["sub"]["id"]
    assert [item["id"] for item in await _mine(client, w)] == sorted(made, reverse=True)
    assert (await _mine(client, w, human_id=made[0]))["id"] == made[0]
    # Предел «3 открытых» не обнулился; повтор по адресу возвращает прежнюю заявку.
    assert (await _create(client, w, url="https://a/9", expect=429))["detail"] == hr.LIMIT_LINE
    again = await _create(client, w, url="https://a/1", expect=200)
    assert (again["created"], again["request"]["id"]) == (False, made[1])
    assert (await _cancel(client, w, made[0]))["status"] == "cancelled"
    await _decide(ui, made[1], "done")
    assert await _wait(client, w, second["sub"]["id"], A) == [
        {"kind": "human", "human_id": made[1], "outcome": "done"}
    ]


async def test_neighbour_on_the_old_line_gets_nothing_while_author_lives(client, ui, db, clock, world):  # noqa: F811
    """Автор жив, но сменил роль; сосед подписался прежней ролью и занял ту же линию: чужой заявки он не
    видит и снять не может, в его предел она не входит (наследование линии после снятия автора — тест
    `test_outcome_signal_once_protocol_8_and_heir_of_the_line`)."""
    w = world
    first = await _sub(client, w, A, role=ROLE)
    made = (await _create(client, w, url="https://a/1"))["request"]["id"]
    await _sub(client, w, A, role=CODER)
    other = await _sub(client, w, B, role=ROLE)
    assert (other["sub"]["role"], other["sub"]["role_no"]) == (ROLE, 1)  # та самая линия
    assert await _mine(client, w, B) == []
    await _mine(client, w, B, human_id=made, expect=404)
    await _cancel(client, w, made, B, expect=404)
    assert _row(db, made).status == "pending"
    # Свой повтор по тому же адресу у соседа — новая заявка, а не чужая; предел считается по своим.
    own = await _create(client, w, B, url="https://a/1")
    assert own["created"] is True and own["request"]["id"] != made
    for i in range(2, 4):
        await _create(client, w, B, url=f"https://a/{i}")
    await _create(client, w, B, url="https://a/9", expect=429)
    # Исход — автору, не соседу.
    await _decide(ui, made, "done")
    assert await _wait(client, w, other["sub"]["id"], B) == []
    assert await _wait(client, w, first["sub"]["id"], A) == [
        {"kind": "human", "human_id": made, "outcome": "done"}
    ]
