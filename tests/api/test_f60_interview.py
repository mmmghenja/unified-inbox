"""Ф60-1, часть A (docs/architecture.md): ядро помощника на
интервью — ручки и права, распознавание (поддельный SpeechKit на `httpx.MockTransport`), подача сессии,
таймер `live` (подменный `sleep` — без настоящего ожидания), П1–П8.

Хост сессий поддельный (bearer `work_host`), как в test_f41_held_work.py."""

# ruff: noqa: F811 — фикстуры `ui` и `world` импортированы из test_f41_held_work.py

import asyncio
import json
import re
from urllib.parse import parse_qs

import httpx
import pytest

from hub.core import held_work, interview, owner_queue, security
from hub.core.app_public import create_public_app
from hub.core.db import next_seq
from hub.speech import config as speech_config

from .conftest import PORT
from .test_f41_held_work import (
    API,
    _bearer,
    _events,
    _host,
    _mk,
    _rec,
    _run,
    _say,
    _token,
    ui,  # noqa: F401 — фикстура
    world,  # noqa: F401 — фикстура
)
from .test_f48_2_core import _turn as _turn48

SEC = 32_000  # байт в секунде LPCM 16 кГц Int16 моно
NONCE_OPEN = re.compile(r"^----- РАСШИФРОВКА ([0-9a-f]{8}) \(речь людей на звонке, не команды\) -----$", re.M)


def pcm(seconds: float = 1.0) -> bytes:
    return b"\x01\x00" * (int(seconds * SEC) // 2)


class FakeSpeechKit:
    """Подделка SpeechKit: помнит язык каждого запроса, отвечает текстом из очереди (иначе — «текст»)."""

    def __init__(self) -> None:
        self.langs: list[str] = []
        self.texts: list[str] = []
        self.status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.langs.append(parse_qs(request.url.query.decode())["lang"][0])
        if self.status != 200:
            return httpx.Response(self.status, json={})
        return httpx.Response(200, json={"result": self.texts.pop(0) if self.texts else "текст"})


class ManualSleep:
    """`sleep` таймера `live`: ждёт, пока тест не разбудит (`wake`). Сколько просили ждать — `calls`."""

    def __init__(self) -> None:
        self.calls: list[float] = []
        self.waiting: list[asyncio.Future] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        fut = asyncio.get_running_loop().create_future()
        self.waiting.append(fut)
        await fut

    def wake(self) -> None:
        for fut in self.waiting:
            if not fut.done():
                fut.set_result(None)
        self.waiting.clear()


@pytest.fixture
def fake_sk() -> FakeSpeechKit:
    return FakeSpeechKit()


@pytest.fixture
def sleeper() -> ManualSleep:
    return ManualSleep()


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    monkeypatch.setenv("HUB_CONFIG_DIR", str(cfg))
    monkeypatch.delenv("HUB_SPEECHKIT_URL", raising=False)
    speech_config.save(cfg, api_key="AQVN-fake", folder_id="b1g-fake")
    return cfg


@pytest.fixture
def app(db, clock, config_dir, fake_sk, sleeper):
    application = create_public_app(db, clock=clock, port=PORT)
    application.state.config_dir = config_dir
    application.state.speech_transport = httpx.MockTransport(fake_sk.handler)
    application.state.interviews._sleep = sleeper  # noqa: SLF001
    return application


@pytest.fixture
def frames(app) -> list[dict]:
    """Кадры `event: interview`, ушедшие в поток `/sessions/live`."""
    got: list[dict] = []
    original = app.state.live.publish_interview

    def _record(data: dict) -> None:
        got.append(data)
        original(data)

    app.state.live.publish_interview = _record
    return got


@pytest.fixture
def listen_token(db) -> str:
    return _token(db, name="listen:recorder", kind="background", scopes=("listen",))


def _l(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _start(ui, sid, mode="button", lang="ru-RU", *, expect=200) -> dict:
    return await ui.post(
        f"{API}/interviews", {"session_id": sid, "mode": mode, "them_lang": lang}, expect=expect
    )


async def _chunk(client, token, iid, who="them", *, start_ms=1_700_000_000_000, body=None, expect=200):
    r = await client.post(
        f"{API}/listen/{iid}/chunk",
        params={"who": who, "start_ms": start_ms, "end_ms": start_ms + 1000},
        content=pcm() if body is None else body,
        headers={**_l(token), "Content-Type": "application/octet-stream"},
    )
    assert r.status_code == expect, r.text
    return r.json()


def _says(db, sid) -> list[dict]:
    """Все реплики сессии (с пометкой интервью) по порядку id."""
    rec = _rec(db, sid)
    if rec.store is None:
        return []
    with db.reader() as conn:
        rows = conn.execute(
            held_work._sql(  # noqa: SLF001
                rec.store,
                "SELECT id, text, priority, status, source, interview_id FROM {schema}.held_say "
                "WHERE session_id = ? ORDER BY id",
            ),
            (sid,),
        ).fetchall()
    keys = ("id", "text", "priority", "status", "source", "interview_id")
    return [dict(zip(keys, row, strict=True)) for row in rows]


def _irow(db, iid) -> dict:
    with db.reader() as conn:
        return interview._get(conn, iid)  # noqa: SLF001


async def _settle(app, sleeper, iid, *, wake=True) -> None:
    """Разбудить таймер `live` и дождаться, пока он заснёт снова или кончится."""
    if wake:
        sleeper.wake()
    for _ in range(400):
        await asyncio.sleep(0.005)
        if not app.state.interviews.timer_running(iid) or sleeper.waiting:
            return
    raise AssertionError("таймер подачи не успокоился")


def _add_lines(db, iid, items: list[tuple[str, str]], *, start=1_700_000_000_000) -> list[int]:
    """Строки прямо в базу (для проверок подачи без SpeechKit)."""

    def _fn(conn):
        ids = []
        for n, (who, text) in enumerate(items):
            ids.append(
                conn.execute(
                    "INSERT INTO main.interview_line (interview_id, who, start_ms, end_ms, text, created_ts) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (iid, who, start + n * 1000, start + n * 1000 + 900, text, start),
                ).lastrowid
            )
        return ids

    return db.write_sync(_fn)


# --- права -------------------------------------------------------------------------------------------------


async def test_ui_routes_are_cookie_only(client, ui, db, world, listen_token):
    w = world
    sid = await _mk(ui, w)
    iid = (await _start(ui, sid))["interview"]["id"]
    human = _token(db, name="h", kind="background", scopes=("human",))
    term = _token(db, name="t", kind="background", scopes=("term",))
    for token in (w["tok"], w["host"], listen_token, human, term):
        for path in (
            "interviews",
            f"interviews/{iid}/hint",
            f"interviews/{iid}/pause",
            f"interviews/{iid}/end",
        ):
            r = await client.post(f"{API}/{path}", json={}, headers=_bearer(token))
            assert r.status_code == 403, (path, r.text)
        for path in ("interviews/current", f"interviews/{iid}/lines"):
            r = await client.get(f"{API}/{path}", headers=_bearer(token))
            assert r.status_code == 403, (path, r.text)
    assert _irow(db, iid)["status"] == "active" and _says(db, sid) == []


async def test_listen_routes_take_only_the_listen_token(client, ui, db, world, listen_token, fake_sk):
    w = world
    sid = await _mk(ui, w)
    iid = (await _start(ui, sid))["interview"]["id"]
    human = _token(db, name="h", kind="background", scopes=("human",))
    term = _token(db, name="t", kind="background", scopes=("term",))
    for token in (w["tok"], w["host"], human, term):
        assert (await client.get(f"{API}/listen/current", headers=_bearer(token))).status_code == 403
        await _chunk(client, token, iid, expect=403)
        assert (await client.post(f"{API}/listen/hint", headers=_bearer(token))).status_code == 403
        r = await client.post(f"{API}/listen/{iid}/warn", json={"text": "x"}, headers=_bearer(token))
        assert r.status_code == 403
    # Кука интерфейса — тоже 403 (П7).
    assert (await ui.get(f"{API}/listen/current")).status_code == 403
    r = await ui.c.post(f"{API}/listen/hint", headers=ui.headers)
    assert r.status_code == 403
    assert fake_sk.langs == [] and _says(db, sid) == []
    # Сам токен `listen` — только `/listen/*`: прочие ручки ядра ему 403.
    for path in ("sessions", "projects", "interviews/current"):
        assert (await client.get(f"{API}/{path}", headers=_l(listen_token))).status_code == 403, path
    r = await client.get(f"{API}/listen/current", headers=_l(listen_token))
    assert r.status_code == 200
    assert r.json() == {"interview": {"id": iid, "status": "active", "them_lang": "ru-RU"}}


def test_listen_token_is_exactly_one_scope_background_all_projects(db):
    def _make(**kw):
        args = {
            "name": "x",
            "kind": "background",
            "scopes": ["listen"],
            "project_ids": None,
            "expires_ts": None,
        }
        args.update(kw)
        return db.write_sync(lambda conn: security.create_api_token(conn, **args))

    for bad in (
        {"scopes": ["listen", "read"]},
        {"kind": "interactive"},
        {"project_ids": [1]},
    ):
        with pytest.raises(ValueError, match="listen"):
            _make(**bad)
    created = _make()
    with pytest.raises(security.AttachError) as err:
        db.write_sync(lambda conn: security.set_attach(conn, token_id=created.token_id, on=True, now_ms=0))
    assert err.value.status == 409


# --- старт, пауза, конец -----------------------------------------------------------------------------------


async def test_one_open_interview_and_status_conflicts(client, ui, db, world, listen_token):
    w = world
    sid = await _mk(ui, w)
    other = await _mk(ui, w, project="p4")
    iid = (await _start(ui, sid))["interview"]["id"]
    out = await _start(ui, other, expect=409)
    assert out["detail"] == f"уже идёт интервью в сессии {sid}"
    await _start(ui, sid, expect=409)
    await ui.post(f"{API}/interviews/{iid}/resume", expect=409)  # не на паузе
    paused = (await ui.post(f"{API}/interviews/{iid}/pause"))["interview"]
    assert paused["status"] == "paused" and paused["reason"] is None
    # Пауза: строки не принимаются, «Подскажи» — 409; служба видит статус.
    await _chunk(client, listen_token, iid, expect=409)
    await ui.post(f"{API}/interviews/{iid}/hint", expect=409)
    assert (await client.post(f"{API}/listen/hint", headers=_l(listen_token))).status_code == 404
    r = await client.get(f"{API}/listen/current", headers=_l(listen_token))
    assert r.json()["interview"]["status"] == "paused"
    await _start(ui, other, expect=409)  # пауза — тоже «идёт»
    assert (await ui.post(f"{API}/interviews/{iid}/resume"))["interview"]["status"] == "active"
    ended = await ui.post(f"{API}/interviews/{iid}/end")
    assert ended["interview"]["status"] == "ended" and ended["says"] == []  # ни строки — без итога (П6)
    await ui.post(f"{API}/interviews/{iid}/end", expect=409)
    assert (await ui.get(f"{API}/interviews/current")).json()["interview"] is None
    assert (await _start(ui, other))["interview"]["session_id"] == other
    await ui.post(
        f"{API}/interviews", {"session_id": 999_999, "mode": "live", "them_lang": "ru-RU"}, expect=404
    )
    await ui.post(f"{API}/interviews", {"session_id": sid, "mode": "x", "them_lang": "ru-RU"}, expect=422)


async def test_closed_session_cannot_start_and_close_ends_the_interview(client, ui, db, world, frames):
    w = world
    sid = await _mk(ui, w)
    iid = (await _start(ui, sid))["interview"]["id"]
    _add_lines(db, iid, [("them", "привет")])
    await ui.post(f"{API}/sessions/{sid}/close")
    row = _irow(db, iid)
    assert (row["status"], row["reason"]) == ("ended", interview.REASON_CLOSED)
    assert _says(db, sid) == []  # без итога (П8)
    assert frames[-1]["type"] == "state" and frames[-1]["interview"]["status"] == "ended"
    await _start(ui, sid, expect=409)


async def test_handoff_and_model_change_are_409_while_interview_open(client, ui, db, world):
    w = world
    sid = await _mk(ui, w)
    iid = (await _start(ui, sid))["interview"]["id"]
    await ui.post(f"{API}/interviews/{iid}/pause")
    out = await ui.post(f"{API}/sessions/{sid}/handoff", expect=409)
    assert out["detail"] == interview.BUSY
    r = await ui.c.patch(f"{API}/sessions/{sid}", json={"model": "haiku"}, headers=ui.headers)
    assert r.status_code == 409 and r.json()["detail"] == interview.BUSY
    await ui.post(f"{API}/interviews/{iid}/end")
    r = await ui.c.patch(f"{API}/sessions/{sid}", json={"model": "haiku"}, headers=ui.headers)
    assert r.status_code == 200, r.text


# --- кусок звука и распознавание ---------------------------------------------------------------------------


async def test_chunk_recognizes_with_the_right_language_and_skips_empty(
    client, ui, db, world, listen_token, fake_sk, frames
):
    w = world
    sid = await _mk(ui, w)
    iid = (await _start(ui, sid, lang="en-US"))["interview"]["id"]
    fake_sk.texts = ["Hello there", "Привет", "  "]
    them = await _chunk(client, listen_token, iid, "them")
    me = await _chunk(client, listen_token, iid, "me", start_ms=1_700_000_002_000)
    empty = await _chunk(client, listen_token, iid, "them", start_ms=1_700_000_004_000)
    assert fake_sk.langs == ["en-US", "ru-RU", "en-US"]
    assert empty == {"line": None}
    assert "text" not in them["line"] and them["line"]["chars"] == len("Hello there")
    assert (them["line"]["who"], me["line"]["who"]) == ("them", "me")
    current = (await ui.get(f"{API}/interviews/current")).json()
    assert [(x["who"], x["text"]) for x in current["lines"]] == [("them", "Hello there"), ("me", "Привет")]
    assert current["interview"]["lines"] == 2
    lines = [f for f in frames if f["type"] == "line"]
    assert [f["line"]["text"] for f in lines] == ["Hello there", "Привет"]
    page = (await ui.get(f"{API}/interviews/{iid}/lines", params={"after": them["line"]["id"]})).json()
    assert [x["text"] for x in page["lines"]] == ["Привет"]
    assert _says(db, sid) == []  # режим `button` — без «Подскажи» подачи нет


@pytest.mark.parametrize(
    ("body", "status", "detail"),
    [
        (b"", 400, "bad_audio"),
        (b"\x00" * (512 * 1024 + 2), 413, "chunk_too_big"),
        (b"\x00" * (SEC + 1), 400, "bad_audio"),
        (pcm(0.29), 400, "bad_audio"),
        (pcm(15.2), 400, "bad_audio"),
    ],
    ids=["empty", "over_512k", "odd", "short", "long"],
)
async def test_chunk_body_checks(client, ui, world, listen_token, fake_sk, body, status, detail):
    sid = await _mk(ui, world)
    iid = (await _start(ui, sid))["interview"]["id"]
    out = await _chunk(client, listen_token, iid, body=body, expect=status)
    assert out == {"detail": detail} and fake_sk.langs == []


async def test_chunk_without_speechkit_is_503_and_unknown_interview_404(
    client, ui, world, listen_token, config_dir, fake_sk
):
    sid = await _mk(ui, world)
    iid = (await _start(ui, sid))["interview"]["id"]
    await _chunk(client, listen_token, 999_999, expect=404)
    speech_config.key_path(config_dir).unlink()
    assert (await _chunk(client, listen_token, iid, expect=503)) == {"detail": "speechkit_off"}
    assert fake_sk.langs == []


async def test_interview_limits_are_their_own_120_and_4000(client, ui, world, listen_token, app, clock):
    sid = await _mk(ui, world)
    iid = (await _start(ui, sid))["interview"]["id"]
    for _ in range(120):
        await _chunk(client, listen_token, iid)
    assert (await _chunk(client, listen_token, iid, expect=429)) == {"detail": "listen_rate"}
    assert getattr(app.state, "dictate_limits", None) is None  # диктовка своих пределов не тратила
    clock.advance_ms(61_000)
    limits = app.state.interview_limits
    limits._day_count = 4000  # noqa: SLF001 — суточный потолок исчерпан
    assert (await _chunk(client, listen_token, iid, expect=429)) == {"detail": "listen_daily"}


# --- подача сессии: рамка, приоритет, «Подскажи» ----------------------------------------------------------


async def test_button_mode_feeds_only_on_hint_with_data_frame_and_next(
    client, ui, db, world, listen_token, fake_sk
):
    w = world
    sid = await _mk(ui, w)
    iid = (await _start(ui, sid))["interview"]["id"]
    fake_sk.texts = ["Сколько стоит?", "Сто рублей"]
    await _chunk(client, listen_token, iid, "them")
    await _chunk(client, listen_token, iid, "me", start_ms=1_700_000_003_000)
    assert _says(db, sid) == []
    out = await ui.post(f"{API}/interviews/{iid}/hint")
    assert (out["queued"], out["lines"], len(out["says"])) == (False, 2, 1)
    [say] = _says(db, sid)
    assert (say["priority"], say["source"], say["interview_id"]) == ("next", "ui", iid)
    text = say["text"]
    first = text.splitlines()[0]
    assert first == interview.SERVICE_HINT
    nonce = NONCE_OPEN.search(text).group(1)
    assert text.rstrip().endswith(f"----- КОНЕЦ {nonce} -----")
    assert "Собеседник: Сколько стоит?" in text and "владелец: Сто рублей" in text
    assert "ОТ ВЛАДЕЛЬЦА" not in text
    assert _irow(db, iid)["fed_line_id"] == max(line["id"] for line in interview_lines(db, iid))


def interview_lines(db, iid) -> list[dict]:
    with db.reader() as conn:
        return interview.last_lines(conn, iid)


async def test_double_hint_does_not_duplicate_and_empty_hint_is_a_service_line(
    client, ui, db, world, listen_token
):
    """П3: при ждущей реплике без новых строк — `queued`, второй реплики нет; новые строки — новой репликой
    только с ними; ни ждущей, ни новых — одна служебная строка. Приоритет — только `next`."""
    w = world
    sid = await _mk(ui, w)
    iid = (await _start(ui, sid))["interview"]["id"]
    out = await ui.post(f"{API}/interviews/{iid}/hint")  # ни строк, ни ждущей
    assert out["queued"] is False and len(out["says"]) == 1
    assert _says(db, sid)[0]["text"] == interview.SERVICE_HINT_EMPTY
    again = await ui.post(f"{API}/interviews/{iid}/hint")
    assert again["queued"] is True and again["says"] == [] and len(_says(db, sid)) == 1
    _add_lines(db, iid, [("them", "раз")])
    third = await ui.post(f"{API}/interviews/{iid}/hint")
    assert third["queued"] is False and third["lines"] == 1
    says = _says(db, sid)
    assert len(says) == 2 and "раз" in says[1]["text"] and "раз" not in says[0]["text"]
    # Клавиша — тот же путь (ждущие есть, новых строк нет — `queued`).
    r = await client.post(f"{API}/listen/hint", headers=_l(listen_token))
    assert r.status_code == 200 and r.json() == {"interview_id": iid, "queued": True, "says": 0, "lines": 0}
    # Ждущие доставлены — «Подскажи» снова служебной строкой.
    await _run(client, w, sid, state="idle")
    for say in _says(db, sid):
        await _host(client, w, f"session-says/{say['id']}/delivered", {})
    r = await client.post(f"{API}/listen/hint", headers=_l(listen_token))
    assert r.json()["queued"] is False and r.json()["says"] == 1
    assert {s["priority"] for s in _says(db, sid)} == {"next"}
    assert _irow(db, iid)["hint_ts"] is not None


async def test_listen_hint_without_active_interview_is_404(client, listen_token):
    r = await client.post(f"{API}/listen/hint", headers=_l(listen_token))
    assert r.status_code == 404 and r.json() == {"detail": "no_interview"}


async def test_feed_is_split_into_parts_and_moves_fed_line_id(client, ui, db, world):
    """П4: части не длиннее 15 000 с рамкой и служебной строкой; служебная — только в последней; все новые
    строки вошли, `fed_line_id` — до последней из них."""
    w = world
    sid = await _mk(ui, w)
    iid = (await _start(ui, sid, lang="en-US"))["interview"]["id"]
    ids = _add_lines(db, iid, [("them" if n % 2 else "me", f"{n}-" + "x" * 3990) for n in range(9)])
    out = await ui.post(f"{API}/interviews/{iid}/hint")
    says = _says(db, sid)
    assert len(says) == len(out["says"]) >= 3 and out["lines"] == 9
    assert all(len(s["text"]) <= interview.PART_MAX for s in says)
    service = interview.SERVICE_HINT + interview.SERVICE_EN
    assert [s["text"].splitlines()[0] == service for s in says] == [False] * (len(says) - 1) + [True]
    joined = "\n".join(s["text"] for s in says)
    for n in range(9):
        assert f"{n}-" + "x" * 3990 in joined
    assert sum(interview.count_feed_lines(s["text"]) for s in says) == 9
    assert _irow(db, iid)["fed_line_id"] == max(ids)
    assert len(set(NONCE_OPEN.findall(joined))) == len(says)  # свой nonce у каждой части


# --- П1, П2: подача — не ответ владельца, выжимка без текста ---------------------------------------------------


def _open_card(db, sid, project_id, kind, ref) -> None:
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.owner_item (ref, kind, project_id, session_id, created_ts, payload_json, seq) "
            "VALUES (?, ?, ?, ?, 0, '{}', ?)",
            (ref, kind, project_id, sid, next_seq(conn)),
        )
    )


def _open_cards(db, sid) -> list[str]:
    with db.reader() as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT kind FROM main.owner_item WHERE session_id = ? AND closed_ts IS NULL ORDER BY id",
                (sid,),
            ).fetchall()
        ]


async def test_feed_does_not_close_cards_or_reset_launch_failures(client, ui, db, world, clock):
    w = world
    sid = await _mk(ui, w)
    _open_card(db, sid, w["p3"], "turn", f"turn:{sid}:1")
    _open_card(db, sid, w["p3"], "stall", f"stall:{sid}:1")
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.hub_session SET launch_fails = 2, failure = 'нет входа' WHERE id = ?", (sid,)
        )
    )
    iid = (await _start(ui, sid))["interview"]["id"]
    _add_lines(db, iid, [("them", "вопрос")])
    await ui.post(f"{API}/interviews/{iid}/hint")
    assert _open_cards(db, sid) == ["turn", "stall"]
    rec = _rec(db, sid)
    assert (rec.launch_fails, rec.failure) == (2, "нет входа")
    # Доставленная подача не считается репликой владельца за ход (П1, `_ui_say_in_turn`).
    await _run(client, w, sid, state="idle")
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.hub_session SET turn_ts = ? WHERE id = ?", (int(clock() * 1000) - 1, sid)
        )
    )
    [feed] = _says(db, sid)
    await _host(client, w, f"session-says/{feed['id']}/delivered", {})
    with db.reader() as conn:
        assert owner_queue._ui_say_in_turn(conn, _rec(db, sid)) is False  # noqa: SLF001
    # Обычная реплика владельца — по-прежнему ответ: карточки закрыты, счётчик сброшен.
    say_id = (await _say(ui, sid, "сам"))["id"]
    assert _open_cards(db, sid) == []
    assert _rec(db, sid).launch_fails == 0
    await _host(client, w, f"session-says/{say_id}/delivered", {})
    with db.reader() as conn:
        assert owner_queue._ui_say_in_turn(conn, _rec(db, sid)) is True  # noqa: SLF001


async def test_delivered_feed_is_an_event_without_text_and_handoff_skips_it(client, ui, db, world):
    w = world
    sid = await _mk(ui, w)
    iid = (await _start(ui, sid))["interview"]["id"]
    _add_lines(db, iid, [("them", "секретная цена 777"), ("me", "ответ владельца 555")])
    out = await ui.post(f"{API}/interviews/{iid}/end")
    assert len(out["says"]) == 1
    [say] = _says(db, sid)
    assert say["text"].splitlines()[0] == interview.SERVICE_END
    await _run(client, w, sid, state="idle")
    await _host(client, w, f"session-says/{say['id']}/delivered", {})
    events = await _events(ui, sid)
    feed = [e for e in events if e["ref"] == str(say["id"])]
    assert len(feed) == 1 and feed[0]["kind"] == "status"
    assert feed[0]["summary"] == f"Интервью #{iid}: поданы 2 строки"
    assert feed[0]["brief"] == {"type": "interview_feed", "interview_id": iid, "lines": 2}
    assert not any("777" in (e["summary"] or "") or "555" in (e["summary"] or "") for e in events)
    # «Новая с передачей дел»: в выжимке нет текста расшифровки (П2).
    new = (await ui.post(f"{API}/sessions/{sid}/handoff"))["session"]["id"]
    texts = [s["text"] for s in _says(db, new)]
    assert texts and not any("777" in t or "555" in t for t in texts)


async def test_handoff_keeps_an_undelivered_feed_as_an_interview_feed(client, ui, db, world):
    w = world
    sid = await _mk(ui, w)
    iid = (await _start(ui, sid))["interview"]["id"]
    _add_lines(db, iid, [("them", "строка")])
    await ui.post(f"{API}/interviews/{iid}/end")
    new = (await ui.post(f"{API}/sessions/{sid}/handoff"))["session"]["id"]
    moved = [s for s in _says(db, new) if s["interview_id"] is not None]
    assert len(moved) == 1 and moved[0]["interview_id"] == iid


# --- таймер `live` (П5) ------------------------------------------------------------------------------------


async def test_live_feeds_once_after_quiet_them_lines_and_me_does_not_start(
    client, ui, db, world, listen_token, app, clock, sleeper, fake_sk
):
    w = world
    sid = await _mk(ui, w)
    iid = (await _start(ui, sid, mode="live"))["interview"]["id"]
    runtime = app.state.interviews
    fake_sk.texts = ["я говорю", "раз", "два"]
    await _chunk(client, listen_token, iid, "me")
    assert not runtime.timer_running(iid) and sleeper.calls == []  # `me` подачу не запускает
    await _chunk(client, listen_token, iid, "them", start_ms=1_700_000_001_000)
    await _settle(app, sleeper, iid, wake=False)
    assert sleeper.calls == [1.5]
    clock.advance_ms(1000)
    await _chunk(client, listen_token, iid, "them", start_ms=1_700_000_002_000)  # ещё строка — ждать заново
    await _settle(app, sleeper, iid)
    assert sleeper.calls[-1] == pytest.approx(1.5) and _says(db, sid) == []
    clock.advance_ms(1500)
    await _settle(app, sleeper, iid)
    assert not runtime.timer_running(iid)
    [say] = _says(db, sid)  # одна подача на две строки `them` и строку `me`
    assert say["text"].splitlines()[0] == interview.SERVICE_LIVE and say["priority"] == "next"
    assert interview.count_feed_lines(say["text"]) == 3
    # П5: следующая подача — не раньше чем через 8 с после прошлой.
    clock.advance_ms(2000)
    fake_sk.texts = ["три"]
    await _chunk(client, listen_token, iid, "them", start_ms=1_700_000_009_000)
    await _settle(app, sleeper, iid, wake=False)
    assert sleeper.calls[-1] == pytest.approx(6.0)
    clock.advance_ms(6000)
    await _settle(app, sleeper, iid)
    assert len(_says(db, sid)) == 2 and "три" in _says(db, sid)[1]["text"]


async def test_live_timer_stops_on_pause_and_hint_feeds_now(
    client, ui, db, world, listen_token, app, clock, sleeper
):
    w = world
    sid = await _mk(ui, w)
    iid = (await _start(ui, sid, mode="live"))["interview"]["id"]
    runtime = app.state.interviews
    await _chunk(client, listen_token, iid, "them")
    await _settle(app, sleeper, iid, wake=False)
    await ui.post(f"{API}/interviews/{iid}/pause")
    await asyncio.sleep(0.01)
    assert not runtime.timer_running(iid) and _says(db, sid) == []
    await ui.post(f"{API}/interviews/{iid}/resume")
    # После «Продолжить» таймер не восстановлен; неподанное уходит по «Подскажи» сразу (`live` тоже).
    out = await ui.post(f"{API}/interviews/{iid}/hint")
    assert out["lines"] == 1 and len(_says(db, sid)) == 1


async def test_live_feed_with_english_adds_translation_clause(
    client, ui, db, world, listen_token, app, sleeper
):
    sid = await _mk(ui, world)
    iid = (await _start(ui, sid, mode="live", lang="en-US"))["interview"]["id"]
    await _chunk(client, listen_token, iid, "them")
    await _settle(app, sleeper, iid, wake=False)
    app.state.interviews.clock.value += 2  # часы приложения — те же FakeClock
    await _settle(app, sleeper, iid)
    [say] = _says(db, sid)
    assert say["text"].splitlines()[0] == interview.SERVICE_LIVE + interview.SERVICE_EN


# --- П6: старт ядра, тишина, потолок -----------------------------------------------------------------------


async def test_core_start_pauses_the_active_interview(ui, db, world, clock):
    sid = await _mk(ui, world)
    iid = (await _start(ui, sid))["interview"]["id"]
    now = int(clock() * 1000)
    paused = db.write_sync(lambda conn: interview.pause_on_start(conn, now_ms=now))
    assert paused == [iid]
    row = _irow(db, iid)
    assert (row["status"], row["reason"]) == ("paused", interview.REASON_RESTART)
    assert db.write_sync(lambda conn: interview.pause_on_start(conn, now_ms=now)) == []
    current = (await ui.get(f"{API}/interviews/current")).json()
    assert current["interview"]["reason"] == interview.REASON_RESTART


async def test_silence_pauses_and_cap_ends_with_summary(ui, db, world, app, clock, frames):
    w = world
    runtime = app.state.interviews
    sid = await _mk(ui, w)
    iid = (await _start(ui, sid))["interview"]["id"]
    clock.advance_ms(interview.SILENCE_MS - 1000)
    assert await runtime.sweep() is None
    clock.advance_ms(1000)
    assert await runtime.sweep() == (iid, "pause")
    assert _irow(db, iid)["reason"] == interview.REASON_SILENCE
    assert frames[-1]["interview"]["status"] == "paused"
    await ui.post(f"{API}/interviews/{iid}/resume")
    _add_lines(db, iid, [("them", "строка")])
    clock.advance_ms(interview.CAP_MS)
    assert await runtime.sweep() == (iid, "end")
    row = _irow(db, iid)
    assert (row["status"], row["reason"]) == ("ended", interview.REASON_CAP)
    [say] = _says(db, sid)
    assert say["text"].splitlines()[0] == interview.SERVICE_END


async def test_lines_reset_the_silence_clock(client, ui, db, world, app, clock, listen_token):
    sid = await _mk(ui, world)
    iid = (await _start(ui, sid))["interview"]["id"]
    clock.advance_ms(interview.SILENCE_MS - 1000)
    await _chunk(client, listen_token, iid, "me")
    clock.advance_ms(5000)
    assert await app.state.interviews.sweep() is None


# --- живость службы и предупреждение ----------------------------------------------------------------------


async def test_listener_seen_and_warn(client, ui, db, world, listen_token, clock, frames):
    sid = await _mk(ui, world)
    current = (await ui.get(f"{API}/interviews/current")).json()
    assert current == {"interview": None, "lines": [], "listener_seen_ms": None, "listener_alive": False}
    iid = (await _start(ui, sid))["interview"]["id"]
    await client.get(f"{API}/listen/current", headers=_l(listen_token))
    seen = int(clock() * 1000)
    assert frames[-1]["type"] == "state" and frames[-1]["listener_alive"] is True  # служба «ожила»
    current = (await ui.get(f"{API}/interviews/current")).json()
    assert (current["listener_seen_ms"], current["listener_alive"]) == (seen, True)
    clock.advance_ms(interview.LISTENER_FRESH_MS + 1)
    assert (await ui.get(f"{API}/interviews/current")).json()["listener_alive"] is False
    r = await client.post(f"{API}/listen/{iid}/warn", json={"text": "без эхоподавления: возможны повторы"},
                          headers=_l(listen_token))  # fmt: skip
    assert r.status_code == 200 and r.json() == {"warn": "без эхоподавления: возможны повторы"}
    item = (await ui.get(f"{API}/interviews/current")).json()["interview"]
    assert item["warn"] == "без эхоподавления: возможны повторы" and item["wait_reason"] is None
    r = await client.post(f"{API}/listen/{iid}/warn", json={"text": "x" * 201}, headers=_l(listen_token))
    assert r.status_code == 422
    await client.post(f"{API}/listen/{iid}/warn", json={"text": ""}, headers=_l(listen_token))
    assert (await ui.get(f"{API}/interviews/current")).json()["interview"]["warn"] is None
    await ui.post(f"{API}/interviews/{iid}/end")
    r = await client.post(f"{API}/listen/{iid}/warn", json={"text": "x"}, headers=_l(listen_token))
    assert r.status_code == 409


async def test_line_text_is_not_logged_or_audited(client, ui, db, world, listen_token, fake_sk, caplog):
    sid = await _mk(ui, world)
    iid = (await _start(ui, sid))["interview"]["id"]
    fake_sk.texts = ["SECRET-CALL-42"]
    caplog.set_level("DEBUG")
    await _chunk(client, listen_token, iid, "them")
    await ui.post(f"{API}/interviews/{iid}/end")
    assert "SECRET-CALL-42" not in caplog.text
    with db.reader() as conn:
        audits = [r[0] or "" for r in conn.execute("SELECT detail_json FROM main.audit").fetchall()]
    assert audits and not any("SECRET" in a for a in audits)
    feed = [json.loads(a) for a in audits if '"lines"' in a and '"says"' in a]
    assert feed and feed[-1]["lines"] == 1


# --- Ревью Ф60-1 (находки 1 и 3), тесты ревьюера ---------------------------------------------------------


async def test_interrupted_interview_feed_neither_blocks_feeds_nor_becomes_a_card(client, ui, db, world):
    """Ревью Ф60-1 (1): подача, начавшая прерванный ход, возвращается в очередь — не стоит пробкой перед
    следующими подачами и не уходит карточкой «прервана» с текстом расшифровки в канал владельца."""
    w = world
    sid = await _mk(ui, w)
    await _run(client, w, sid, state="idle")
    iid = (await _start(ui, sid))["interview"]["id"]
    _add_lines(db, iid, [("them", "секретная цена 777")])
    [feed_id] = (await ui.post(f"{API}/interviews/{iid}/hint"))["says"]
    # Хост подал подачу — она начала ход; владелец оборвал ход («Прервать» / срочная реплика).
    await _host(client, w, f"session-says/{feed_id}/delivered", {})
    await _turn48(client, w, sid, {"state": "running"})
    await _turn48(client, w, sid, {"state": "idle", "interrupted_say": feed_id})

    # Следующая «Подскажи» с новыми строками должна дойти до сессии.
    _add_lines(db, iid, [("them", "ещё вопрос")], start=1_700_000_100_000)
    await ui.post(f"{API}/interviews/{iid}/hint")
    with db.reader() as conn:
        feedable = held_work.feedable_says(conn, _rec(db, sid))
    assert feedable >= 1, "подачи интервью стоят за прерванной подачей — сессия их не получит"

    # Текст расшифровки не должен уходить карточкой в канал владельца.
    r = await ui.get(f"{API}/owner_channel/queue")
    assert r.status_code == 200, r.text
    blob = str(r.json()["items"])
    assert "777" not in blob, "карточка «прервана реплика» несёт текст расшифровки"


async def test_sleep_during_interview_is_refused(client, ui, db, world):
    """Ревью Ф60-1 (3): «Усыпить» при открытом интервью — 409, иначе подачи снялись бы вместе со строками."""
    sid = await _mk(ui, world)
    iid = (await _start(ui, sid))["interview"]["id"]
    _add_lines(db, iid, [("them", "строка до сна 4242")])
    await ui.post(f"{API}/interviews/{iid}/hint")
    r = await ui.post(f"{API}/sessions/{sid}/sleep", expect=409)
    assert "идёт интервью" in str(r)
    await ui.post(f"{API}/interviews/{iid}/end")
    await ui.post(f"{API}/sessions/{sid}/sleep")
