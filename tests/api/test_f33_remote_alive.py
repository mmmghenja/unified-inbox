"""Ф33-1 (docs/architecture.md и 12а К1, К2, К9; ревью Б1, В1, П1): живость сессии на
другой машине.

Машина ядра — `HUB_HOST_ID=CORE` (до сборки приложения), сессия «ноутбука» шлёт в теле `session` поле
`host=LAPTOP`. `/proc` подменён таблицей: процессов сессии «ноутбука» на машине ядра нет — живость только по
реестру удалённых личностей (контакт, ожидание, пульс, `gone`). Строки без машины или с машиной ядра реестр
не судит — живость по `/proc`, как до 33-1."""

import pytest
from httpx import ASGITransport, AsyncClient

from hub import hostid
from hub.core import chan_subs, runner, security, session_host, subs
from hub.core.app_public import create_public_app
from hub.core.routes.write import SendSessionIn

from .conftest import BASE_URL, PORT, insert_account, insert_channel, insert_project, login_via_code

CORE = "c0" * 8
LAPTOP = "1a" * 8
OTHER = "0e" * 8
ORIGIN = BASE_URL
PID, START, SID = 4242, 777_000, "sess-aaaaaaaa-1111"
LOCAL_PID, LOCAL_START = 5151, 888_000
OC_PID, OC_SID = 4096, "ses_AAAAAAAAAA1"
MIN_MS = 60_000
HOUR_MS = 60 * MIN_MS
BOOT1 = "11111111-1111-4111-8111-111111111111"
BOOT2 = "22222222-2222-4222-8222-222222222222"
BOOT3 = "33333333-3333-4333-8333-333333333333"


@pytest.fixture(autouse=True)
def core_host(monkeypatch):
    """Машина ядра — CORE (реестр берёт её при сборке приложения)."""
    monkeypatch.setenv(hostid.ENV, CORE)


@pytest.fixture
def procs(monkeypatch):
    """Процессы машины ядра: только локальная сессия LOCAL_PID."""
    table = {LOCAL_PID: LOCAL_START}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


def _token(db, name="mcp:all", scopes=("read", "draft"), project_ids=None):
    return db.write_sync(
        lambda conn: security.create_api_token(
            conn,
            name=name,
            kind="interactive",
            scopes=list(scopes),
            project_ids=project_ids,
            expires_ts=None,
        )
    )


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN, "X-Hub-Sub-Wait": "9"}


def _session(*, pid=PID, start=START, sid=SID, host=LAPTOP, cwd=None, kind="interactive", boot=None):
    body = {"pid": pid, "proc_start": start, "session_id": sid}
    if host is not None:
        body["host"] = host
    if boot is not None:
        body["boot"] = boot
    if cwd is not None:
        body.update({"cwd": cwd, "kind": kind, "name": None, "name_source": None})
    return body


@pytest.fixture
def world(db, tmp_path):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    pid = insert_project(db, name="Umbrella", workdir=str(workdir))
    db.write_sync(lambda conn: subs.set_project_subs(conn, pid, True, now_ms=0))
    acc = insert_account(db, source="tg")
    cid = insert_channel(db, acc, project_id=pid, ext_id="c1", title="Клиент")
    tok = _token(db)
    return {"pid": pid, "workdir": str(workdir), "cid": cid, "tok": tok.token, "tok_id": tok.token_id}


async def _subscribe(client, w, role=None, tok=None, **kw):
    body = {"project_id": None, "session": _session(cwd=w["workdir"], **kw)}
    if role is not None:
        body["role"] = role
    return await client.post("/api/v1/subs", json=body, headers=_h(tok or w["tok"]))


async def _chan_sub(client, w, tok=None, **kw):
    return await client.post(
        "/api/v1/chan-subs",
        json={"channel_id": w["cid"], "session": _session(cwd=w["workdir"], **kw)},
        headers=_h(tok or w["tok"]),
    )


async def _beat(client, w, tok=None, **kw):
    gone = kw.pop("gone", False)
    extra = kw.pop("extra", {})
    body = {"session": _session(**kw), **extra}
    if gone:
        body["gone"] = True
    r = await client.post("/api/v1/subs/beat", json=body, headers=_h(tok or w["tok"]))
    assert r.status_code == 200, r.text
    return r.json()


async def _live(client, w, sub_id) -> bool:
    r = await client.get("/api/v1/subs", headers=_h(w["tok"]))
    assert r.status_code == 200, r.text
    rows = {row["id"]: row for row in r.json()["items"]}
    return rows[sub_id]["live"]


def _cleanup(db, clock):
    now = int(clock() * 1000)
    return db.write_sync(lambda conn: runner._cleanup_all(conn, now))


def _row(db, sub_id):
    with db.reader() as conn:
        return subs.get_sub(conn, sub_id)


def _col(db, table, column, sub_id):
    with db.reader() as conn:
        return conn.execute(f"SELECT {column} FROM main.{table} WHERE id = ?", (sub_id,)).fetchone()[0]  # noqa: S608


def _restart(db, clock):
    """Новый процесс ядра: реестра в памяти нет, засев из базы."""
    session_host.reset()
    session_host._by_db.clear()
    create_public_app(db, clock=clock, port=PORT)


def _evil(db):
    """Другой токен: только read, свой проект — никаких прав на чужие подписки."""
    other_project = insert_project(db, name="Чужой")
    return _token(db, name="mcp:other", scopes=("read",), project_ids=[other_project]).token


# --- машина ------------------------------------------------------------------------------------------------


def test_host_id_env_override_and_machine(monkeypatch):
    assert hostid.host_id({hostid.ENV: LAPTOP}) == LAPTOP
    other = hostid.host_id({hostid.ENV: "стенд-1"})
    assert hostid.HOST_RE.fullmatch(other) and other != LAPTOP
    real = hostid.host_id({})
    assert real is None or hostid.HOST_RE.fullmatch(real)


# --- подписка с другой машины -----------------------------------------------------------------------------


async def test_first_subscribe_without_beat_lives_without_proc(client, db, world, procs):
    """Первый `subscribe` удалённой сессии: в `/proc` ядра её нет, но тело — контакт: подписка живая, машина
    записана в столбец."""
    r = await _subscribe(client, world)
    assert r.status_code == 200, r.text
    sub = r.json()["sub"]
    assert sub["live"] is True
    assert _col(db, "session_sub", "session_host", sub["id"]) == LAPTOP
    assert subs.is_alive(PID, START, LAPTOP)
    assert not subs.is_alive(PID, START)  # без машины строки — `/proc`, а процесса на машине ядра нет
    assert session_host.state(LAPTOP, PID, START) == session_host.LIVE


async def test_local_session_unchanged(client, db, world, procs):
    """Без `host` или со своей машиной — живость по `/proc`, как раньше: мёртвый процесс — 409, живой — да."""
    r = await _subscribe(client, world, host=None)
    assert r.status_code == 409
    r = await _subscribe(client, world, host=CORE)
    assert r.status_code == 409
    assert session_host.state(CORE, PID, START) is None
    r = await _subscribe(client, world, pid=LOCAL_PID, start=LOCAL_START, host=CORE)
    assert r.status_code == 200, r.text
    assert await _beat(client, world, pid=LOCAL_PID, start=LOCAL_START, host=CORE) == {
        "remote": False,
        "subscribed": False,
        "beat": False,
    }
    procs.pop(LOCAL_PID)
    assert _cleanup(db, client._transport.app.state.clock) == [(str(r.json()["sub"]["id"]), "session_gone")]


async def test_stale_is_offline_not_ended_and_beat_revives(client, db, world, procs, clock):
    """К2: обрыв дольше 180 с — «не в сети» (`live=False`), чистка подписку НЕ снимает; пульс той же
    личности снова делает её живой."""
    sub_id = (await _subscribe(client, world)).json()["sub"]["id"]
    clock.advance_ms(session_host.STALE_MS + 1_000)
    assert not subs.is_alive(PID, START, LAPTOP)
    assert await _live(client, world, sub_id) is False
    assert _cleanup(db, clock) == []
    assert _row(db, sub_id).ended_ts is None
    answer = await _beat(client, world)
    assert answer == {"remote": True, "subscribed": True, "beat": True, "neighbours": []}
    assert await _live(client, world, sub_id) is True


async def test_twelve_hours_without_contact_ends(client, db, world, procs, clock):
    sub_id = (await _subscribe(client, world)).json()["sub"]["id"]
    clock.advance_ms(11 * HOUR_MS)
    assert _cleanup(db, clock) == []
    clock.advance_ms(HOUR_MS + 1_000)
    assert _cleanup(db, clock) == [(str(sub_id), "session_gone")]


async def test_gone_ends_project_and_channel_subs(client, db, world, procs):
    """К1: `gone` (процесс сессии мёртв по `/proc` её машины) — подписки процесса сняты сразу, повторный
    контакт его не воскрешает."""
    sub_id = (await _subscribe(client, world)).json()["sub"]["id"]
    chan_id = (await _chan_sub(client, world)).json()["sub"]["id"]
    answer = await _beat(client, world, gone=True)
    assert answer["ended"] == 2 and answer["beat"] is False
    assert _row(db, sub_id).end_reason == "session_gone"
    with db.reader() as conn:
        assert chan_subs.get(conn, chan_id).end_reason == "session_gone"
    await _beat(client, world)
    assert not subs.is_alive(PID, START, LAPTOP)
    r = await _subscribe(client, world)
    assert r.status_code == 409


async def test_beat_without_gone_keeps_subs(client, db, world, procs, clock):
    """`/mcp` и переподключение сервера: MCP не шлёт `gone` (сессия жива) — обычный пульс подписку держит."""
    sub_id = (await _subscribe(client, world)).json()["sub"]["id"]
    for _ in range(5):
        clock.advance_ms(MIN_MS)
        assert (await _beat(client, world))["beat"] is True
    assert _cleanup(db, clock) == []
    assert await _live(client, world, sub_id) is True


async def test_channel_only_subscription_beats(client, db, world, procs, clock):
    """К9: пульс покрывает и сессию с одними подписками на беседы."""
    r = await _chan_sub(client, world)
    assert r.status_code == 200, r.text
    chan_id = r.json()["sub"]["id"]
    assert _col(db, "session_chan_sub", "session_host", chan_id) == LAPTOP
    assert (await _beat(client, world))["beat"] is True
    clock.advance_ms(session_host.STALE_MS + 1_000)
    assert _cleanup(db, clock) == []
    clock.advance_ms(session_host.DROP_MS)
    assert _cleanup(db, clock) == [(f"c{chan_id}", "session_gone")]


async def test_beat_new_conversation_ends_old(client, db, world, procs):
    """`/clear`: пульс с новым `session_id` того же процесса — прежняя подписка `conversation_changed`, слать
    дальше нечего."""
    sub_id = (await _subscribe(client, world)).json()["sub"]["id"]
    answer = await _beat(client, world, sid="sess-bbbbbbbb-2222")
    assert answer == {"remote": True, "subscribed": False, "beat": False, "neighbours": []}
    assert _row(db, sub_id).end_reason == "conversation_changed"


async def test_resume_in_new_process_replaces_offline(client, db, world, procs, clock):
    """Тот же разговор из нового процесса, прежний «не в сети» — прежняя подписка `replaced` (иначе держала
    бы линию до 12 ч)."""
    sub_id = (await _subscribe(client, world, sid=SID)).json()["sub"]["id"]
    clock.advance_ms(session_host.STALE_MS + 1_000)
    r = await _subscribe(client, world, pid=PID + 1, start=START + 1)
    assert r.status_code == 200, r.text
    assert _row(db, sub_id).end_reason == "replaced"


async def test_opencode_remote_skips_process_only_oc_stale(client, db, world, procs, clock):
    """К9: удалённый OpenCode — процесс не проверяется (сервер 4096 держит много сессий), живость — только
    `oc_stale` (10 мин без ожидания)."""
    r = await _subscribe(client, world, pid=OC_PID, start=START, sid=OC_SID, kind="opencode")
    assert r.status_code == 200, r.text
    sub_id = r.json()["sub"]["id"]
    clock.advance_ms(session_host.STALE_MS + 1_000)
    assert subs.is_alive(OC_PID, START, LAPTOP)
    assert _cleanup(db, clock) == []
    clock.advance_ms(subs.OC_LIVE_GRACE_MS)
    assert _cleanup(db, clock) == [(str(sub_id), "session_gone")]


async def test_wait_keeps_alive_and_closed_wait_counts(db, world, clock):
    """Идёт ожидание — жива при любой давности контакта; закрытое ожидание — ещё 180 с."""
    reg = session_host.current()
    reg.own = CORE
    reg.clock = clock
    assert reg.touch(PID, START, SID, LAPTOP)
    assert reg.wait_begin(LAPTOP, PID, START)
    clock.advance_ms(10 * session_host.STALE_MS)
    assert reg.state(LAPTOP, PID, START) == session_host.LIVE
    reg.wait_end(LAPTOP, PID, START)
    clock.advance_ms(session_host.STALE_MS - 1_000)
    assert reg.state(LAPTOP, PID, START) == session_host.LIVE
    clock.advance_ms(2_000)
    assert reg.state(LAPTOP, PID, START) == session_host.STALE


async def test_core_restart_seeds_from_db(client, db, world, procs, clock):
    """После рестарта ядра удалённая подписка свежа 180 с от старта, затем «не в сети», но не снята."""
    sub_id = (await _subscribe(client, world)).json()["sub"]["id"]
    clock.advance_ms(HOUR_MS)
    _restart(db, clock)
    assert subs.is_alive(PID, START, LAPTOP)
    assert _cleanup(db, clock) == []
    clock.advance_ms(session_host.STALE_MS + 1_000)
    assert not subs.is_alive(PID, START, LAPTOP)
    assert _cleanup(db, clock) == []
    assert _row(db, sub_id).ended_ts is None


async def test_any_session_body_is_contact(client, db, world, procs, clock):
    """К9: любой запрос с телом `session` и чужой машиной — контакт (обе модели тела)."""
    await _subscribe(client, world)
    clock.advance_ms(session_host.STALE_MS + 1_000)
    assert not subs.is_alive(PID, START, LAPTOP)
    SendSessionIn(**_session())
    assert subs.is_alive(PID, START, LAPTOP)


async def test_cache_from_beat_for_ui(client, db, world, procs, clock):
    """Кэш и контекст удалённой сессии Claude Code — из пульса (стенограммы на машине ядра нет)."""
    sub_id = (await _subscribe(client, world)).json()["sub"]["id"]
    until = int(clock() * 1000) + 30 * MIN_MS
    await _beat(client, world, extra={"cache_until_ms": until, "context_tokens": 123_456, "ttl_s": 3600})
    async with AsyncClient(transport=ASGITransport(app=client._transport.app), base_url=BASE_URL) as ui:
        await login_via_code(ui, db, clock)
        r = await ui.get("/api/v1/subs")
    assert r.status_code == 200, r.text
    row = next(x for x in r.json()["items"] if x["id"] == sub_id)
    assert row["cache"]["expires_ts"] == until
    assert row["cache"]["context_tokens"] == 123_456
    assert row["cache"]["ttl_s"] == 3600


async def test_bad_host_422(client, db, world, procs, clock):
    r = await client.post(
        "/api/v1/subs/beat", json={"session": _session(host="не hex")}, headers=_h(world["tok"])
    )
    assert r.status_code == 422


async def test_no_contact_without_token(client, db, world, procs):
    """Зависимость принципала решается раньше тела: без годного токена контакта нет."""
    r = await client.post(
        "/api/v1/subs/beat", json={"session": _session()}, headers={"Authorization": "Bearer hubt_bad"}
    )
    assert r.status_code in (401, 403)
    r = await client.post("/api/v1/subs/beat", json={"session": _session()}, headers={"Origin": ORIGIN})
    assert r.status_code in (401, 403)
    assert session_host.state(LAPTOP, PID, START) is None


# --- ревью Б1: чужой токен и чужая машина ------------------------------------------------------------------


async def _sub_local(client, w):
    r = await _subscribe(client, w, pid=LOCAL_PID, start=LOCAL_START, host=CORE)
    assert r.status_code == 200, r.text
    return r.json()["sub"]["id"]


async def test_foreign_token_gone_does_not_end_local_session(client, db, world, procs):
    """Б1 (r1): другой токен шлёт `gone` с pid локальной сессии и выдуманной машиной — строку с машиной ядра
    реестр не судит, `gone` трогает только строки этой машины под этим токеном: снято 0."""
    sub_id = await _sub_local(client, world)
    answer = await _beat(
        client, world, tok=_evil(db), pid=LOCAL_PID, start=LOCAL_START, sid="whatever", gone=True
    )
    assert answer["ended"] == 0
    assert _row(db, sub_id).ended_ts is None
    assert subs.is_alive(LOCAL_PID, LOCAL_START, CORE)


async def test_forged_contact_does_not_make_local_session_remote(client, db, world, procs, clock):
    """Б1 (r2): тело с чужой машиной для pid локальной сессии — живость её строки по-прежнему по `/proc`:
    через 3 мин и 12 ч живая, не снята."""
    sub_id = await _sub_local(client, world)
    await _beat(client, world, tok=_evil(db), pid=LOCAL_PID, start=LOCAL_START, sid="x")
    clock.advance_ms(session_host.STALE_MS + 1_000)
    assert await _live(client, world, sub_id) is True
    clock.advance_ms(session_host.DROP_MS)
    assert _cleanup(db, clock) == []
    assert _row(db, sub_id).ended_ts is None


async def test_foreign_token_new_boot_does_not_end_machine(client, db, world, procs):
    """Б1 (r3): другой токен шлёт тело с машиной ноутбука и выдуманной загрузкой — подписки ноутбука под
    токеном сессии не трогаются."""
    sub_id = (await _subscribe(client, world, boot=BOOT1)).json()["sub"]["id"]
    await _beat(client, world, tok=_evil(db), pid=1, start=1, sid="x", boot=BOOT3)
    assert _row(db, sub_id).ended_ts is None


async def test_gone_and_clear_only_this_token_and_machine(client, db, world, procs):
    """Б1: `gone` и смена разговора снимают только строки той же машины под тем же токеном; строка того же
    процесса под другим токеном живёт, а личность не `gone`, пока у неё есть живые строки."""
    other_tok = _token(db, name="mcp:second").token
    mine = (await _subscribe(client, world)).json()["sub"]["id"]
    r = await _chan_sub(client, world, tok=other_tok)
    assert r.status_code == 200, r.text
    theirs = r.json()["sub"]["id"]
    answer = await _beat(client, world, gone=True)
    assert answer["ended"] == 1
    assert _row(db, mine).end_reason == "session_gone"
    with db.reader() as conn:
        assert chan_subs.get(conn, theirs).ended_ts is None
    assert subs.is_alive(PID, START, LAPTOP)
    await _beat(client, world, sid="sess-new")  # смена разговора под первым токеном — чужую строку не трогает
    with db.reader() as conn:
        assert chan_subs.get(conn, theirs).ended_ts is None
    await _beat(client, world, tok=other_tok, sid="sess-new")
    with db.reader() as conn:
        assert chan_subs.get(conn, theirs).end_reason == "conversation_changed"
    other = await _subscribe(client, world, pid=PID + 2, start=START + 2, sid="s-2", host=OTHER)
    assert other.status_code == 200, other.text
    await _beat(client, world, pid=PID + 2, start=START + 2, sid="s-2", gone=True)  # машина ноутбука
    assert _row(db, other.json()["sub"]["id"]).ended_ts is None


# --- ревью В1: рестарты ядра не продлевают мёртвую подписку ----------------------------------------------


async def test_core_restarts_do_not_renew_dead_remote_session(client, db, world, procs, clock):
    """В1 (r4): сессия умерла без `gone`, ядро перезапускается каждые 11 ч — окно 12 ч от последнего
    сохранённого контакта: подписка снята после 12 ч."""
    sub_id = (await _subscribe(client, world, boot=BOOT1)).json()["sub"]["id"]
    assert _col(db, "session_sub", "session_seen_ts", sub_id) == int(clock() * 1000)
    clock.advance_ms(11 * HOUR_MS)
    assert _cleanup(db, clock) == []
    _restart(db, clock)
    assert _cleanup(db, clock) == []
    clock.advance_ms(HOUR_MS + 1_000)
    _restart(db, clock)
    assert _cleanup(db, clock) == [(str(sub_id), "session_gone")]


async def test_seen_saved_at_most_every_ten_minutes(client, db, world, procs, clock):
    """В1: контакт пишется в строку не чаще раза в 10 мин; пульсующая сессия переживает рестарт после 12 ч."""
    sub_id = (await _subscribe(client, world)).json()["sub"]["id"]
    first = _col(db, "session_sub", "session_seen_ts", sub_id)
    clock.advance_ms(5 * MIN_MS)
    await _beat(client, world)
    assert _col(db, "session_sub", "session_seen_ts", sub_id) == first
    clock.advance_ms(6 * MIN_MS)
    await _beat(client, world)
    assert _col(db, "session_sub", "session_seen_ts", sub_id) == int(clock() * 1000)
    for _ in range(13):
        clock.advance_ms(HOUR_MS)
        await _beat(client, world)
    _restart(db, clock)
    assert _cleanup(db, clock) == []
    assert subs.is_alive(PID, START, LAPTOP)


async def test_seen_written_by_wait(client, db, world, procs, clock):
    """В1: ожидание сессии другой машины — тоже контакт, сохраняется в строку подписки на беседу."""
    chan_id = (await _chan_sub(client, world)).json()["sub"]["id"]
    clock.advance_ms(11 * MIN_MS)
    r = await client.post(
        "/api/v1/chan-subs/wait",
        json={"session": _session(), "waiter": "0" * 16, "timeout_s": 1},
        headers=_h(world["tok"]),
    )
    assert r.status_code in (200, 204), r.text
    assert _col(db, "session_chan_sub", "session_seen_ts", chan_id) == int(clock() * 1000)


# --- финальное ревью Ф33 (17.10): переезд ядра не снимает подписки машины, где ядро жило ----------------

DESK = "de" * 8


async def test_switch_keeps_subs_of_former_core_machine(client, db, world, procs, monkeypatch, clock):
    """Сессия ноутбука, подписанная, пока ядро жило на ноутбуке (`session_host` = машина ядра): чистка, видя
    живой процесс в `/proc`, пишет контакт (не чаще раза в 10 мин). Ядро переехало на стационарник — окно
    12 ч от последнего контакта: подписки «не в сети», не сняты; без связи 12 ч — сняты."""
    kw = {"pid": LOCAL_PID, "start": LOCAL_START, "host": CORE}
    sub_id = (await _subscribe(client, world, **kw)).json()["sub"]["id"]
    chan_id = (await _chan_sub(client, world, **kw)).json()["sub"]["id"]
    t0 = int(clock() * 1000)
    assert _col(db, "session_sub", "session_seen_ts", sub_id) == t0
    assert _col(db, "session_chan_sub", "session_seen_ts", chan_id) == t0
    clock.advance_ms(5 * MIN_MS)
    assert _cleanup(db, clock) == []
    assert _col(db, "session_sub", "session_seen_ts", sub_id) == t0  # рано: раз в 10 мин
    clock.advance_ms(13 * HOUR_MS)
    assert _cleanup(db, clock) == []
    seen = int(clock() * 1000)
    assert _col(db, "session_sub", "session_seen_ts", sub_id) == seen
    assert _col(db, "session_chan_sub", "session_seen_ts", chan_id) == seen
    # переезд: машина ядра — стационарник, процесса ноутбука в его /proc нет
    procs.clear()
    monkeypatch.setenv(hostid.ENV, DESK)
    _restart(db, clock)
    assert session_host.state(CORE, LOCAL_PID, LOCAL_START) == session_host.LIVE
    assert _cleanup(db, clock) == []
    clock.advance_ms(session_host.STALE_MS + 1_000)
    assert session_host.state(CORE, LOCAL_PID, LOCAL_START) == session_host.STALE
    assert _cleanup(db, clock) == []
    clock.advance_ms(12 * HOUR_MS)
    assert sorted(_cleanup(db, clock)) == sorted(
        [(str(sub_id), "session_gone"), (f"c{chan_id}", "session_gone")]
    )


async def test_core_machine_contacts_saved_every_ten_minutes(client, db, world, procs, clock):
    """Контакт сессии своей машины — проба пульса и ожидание — пишется в строки, не чаще раза в 10 мин; сессия
    без машины в теле — нет (её машину не назвать)."""
    kw = {"pid": LOCAL_PID, "start": LOCAL_START, "host": CORE}
    sub_id = (await _subscribe(client, world, **kw)).json()["sub"]["id"]
    chan_id = (await _chan_sub(client, world, **kw)).json()["sub"]["id"]
    t0 = int(clock() * 1000)
    clock.advance_ms(5 * MIN_MS)
    assert (await _beat(client, world, **kw))["remote"] is False
    assert _col(db, "session_sub", "session_seen_ts", sub_id) == t0
    clock.advance_ms(6 * MIN_MS)
    await _beat(client, world, **kw)
    assert _col(db, "session_sub", "session_seen_ts", sub_id) == int(clock() * 1000)
    clock.advance_ms(11 * MIN_MS)
    r = await client.post(
        "/api/v1/chan-subs/wait",
        json={"session": _session(**kw), "waiter": "0" * 16, "timeout_s": 1},
        headers=_h(world["tok"]),
    )
    assert r.status_code in (200, 204), r.text
    assert _col(db, "session_chan_sub", "session_seen_ts", chan_id) == int(clock() * 1000)
    assert session_host.state(CORE, LOCAL_PID, LOCAL_START) is None  # своя машина реестром не судится
    # без машины в теле — контакта в строке нет
    now = int(clock() * 1000)
    db.write_sync(lambda conn: subs.end_sub(conn, sub_id, "unsubscribed", now_ms=now, actor="core"))
    bare = (await _subscribe(client, world, pid=LOCAL_PID, start=LOCAL_START, host=None)).json()["sub"]["id"]
    clock.advance_ms(11 * MIN_MS)
    assert _cleanup(db, clock) == []
    assert _col(db, "session_sub", "session_seen_ts", bare) is None


async def test_seed_takes_latest_of_wait_and_delivery_when_seen_empty(client, db, world, procs, clock):
    """Строки, созданные до записи контакта своей машины (`session_seen_ts` пуст): засев берёт наибольшее из
    `created_ts`, `last_wait_ts`, `last_delivery_ts` — подписка, ждавшая час назад, после переезда жива."""
    sub_id = (await _subscribe(client, world)).json()["sub"]["id"]
    clock.advance_ms(13 * HOUR_MS)
    hour_ago = int(clock() * 1000) - HOUR_MS
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.session_sub SET session_seen_ts = NULL, last_wait_ts = ? WHERE id = ?",
            (hour_ago, sub_id),
        )
    )
    _restart(db, clock)
    assert session_host.current().get(LAPTOP, PID, START).last_ms == hour_ago
    assert _cleanup(db, clock) == []
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.session_sub SET last_wait_ts = NULL, last_delivery_ts = ? WHERE id = ?",
            (hour_ago + 1, sub_id),
        )
    )
    _restart(db, clock)
    assert session_host.current().get(LAPTOP, PID, START).last_ms == hour_ago + 1
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.session_sub SET last_delivery_ts = NULL WHERE id = ?", (sub_id,)
        )
    )
    _restart(db, clock)  # ничего, кроме `created_ts` 13 ч назад, — мертва
    assert _cleanup(db, clock) == [(str(sub_id), "session_gone")]


# --- ревью П1: соседи той же машины, закрытое окно без `gone` -----------------------------------------------


async def test_beat_names_neighbours_and_client_gone_frees_role_line(client, db, world, procs, clock):
    """П1 (r5): сессия роли умерла без `gone` (закрыто окно, та же загрузка). Первый пульс новой сессии той
    же машины и токена называет её соседом; клиент по своему `/proc` шлёт за неё `gone` — линия роли свободна,
    новая подписка — №1, а не «роль 2»."""
    r = await _subscribe(client, world, role="маркетолог", boot=BOOT1)
    old_id = r.json()["sub"]["id"]
    other_tok = _token(db, name="mcp:second").token
    assert (
        await _subscribe(client, world, tok=other_tok, pid=PID + 9, start=START + 9, sid="s9")
    ).status_code == 200
    assert (
        await _subscribe(client, world, pid=PID + 8, start=START + 8, sid="s8", host=OTHER)
    ).status_code == 200
    clock.advance_ms(30 * MIN_MS)
    new = {"pid": PID + 5, "start": START + 5, "sid": "sess-new", "boot": BOOT1}
    answer = await _beat(client, world, **new)
    assert answer["neighbours"] == [{"pid": PID, "proc_start": START, "session_id": SID}]
    gone = await _beat(client, world, pid=PID, start=START, sid=SID, gone=True)
    assert gone["ended"] == 1
    assert _row(db, old_id).end_reason == "session_gone"
    r = await _subscribe(client, world, role="маркетолог", **new)
    assert r.status_code == 200, r.text
    assert r.json()["sub"]["role_no"] == 1


# --- загрузка машины (boot): ноутбук выключался ------------------------------------------------------------


def test_boot_id_env_override_and_file():
    assert hostid.boot_id({hostid.BOOT_ENV: BOOT1}) == BOOT1
    other = hostid.boot_id({hostid.BOOT_ENV: "загрузка-2"})
    assert hostid.BOOT_RE.fullmatch(other) and other != BOOT1
    real = hostid.boot_id({})
    assert real is None or hostid.BOOT_RE.fullmatch(real)


async def test_reboot_frees_role_line_with_session_gone(client, db, world, procs, clock):
    """Сессия роли подписана в загрузке 1, ноутбук выключили (подписки «не в сети»), после включения новая
    сессия той же роли из загрузки 2 подписывается: подписки прежней загрузки (на проект и на беседу) сняты
    `session_gone`, новая — первая линия роли, не «роль 2»."""
    r = await _subscribe(client, world, role="маркетолог", boot=BOOT1)
    assert r.status_code == 200, r.text
    old_id = r.json()["sub"]["id"]
    assert (await _chan_sub(client, world, boot=BOOT1)).status_code == 200
    with db.reader() as conn:
        chan_old = chan_subs.live_of_process(conn, PID, START)[0]
    assert _col(db, "session_sub", "session_boot", old_id) == BOOT1
    clock.advance_ms(8 * HOUR_MS)
    r = await _subscribe(
        client, world, role="маркетолог", pid=PID + 7, start=START + 7, sid="sess-new", boot=BOOT2
    )
    assert r.status_code == 200, r.text
    assert r.json()["sub"]["role_no"] == 1
    assert _row(db, old_id).end_reason == "session_gone"
    with db.reader() as conn:
        assert chan_subs.get(conn, chan_old.id).end_reason == "session_gone"
    assert not subs.is_alive(PID, START, LAPTOP)
    assert subs.is_alive(PID + 7, START + 7, LAPTOP)


async def test_same_boot_and_other_machine_untouched(client, db, world, procs, clock):
    """Вторая сессия той же загрузки и новая загрузка ДРУГОЙ машины прежние подписки не трогают."""
    first = (await _subscribe(client, world, boot=BOOT1)).json()["sub"]["id"]
    other = await _subscribe(client, world, pid=PID + 1, start=START + 1, sid="s-o", host=OTHER, boot=BOOT1)
    assert other.status_code == 200, other.text
    other_id = other.json()["sub"]["id"]
    r = await _subscribe(client, world, pid=PID + 2, start=START + 2, sid="s-2", boot=BOOT1)
    assert r.status_code == 200, r.text
    assert _row(db, first).ended_ts is None
    r = await _subscribe(client, world, pid=PID + 3, start=START + 3, sid="s-3", boot=BOOT2)
    assert r.status_code == 200, r.text
    assert _row(db, first).end_reason == "session_gone"
    assert _row(db, other_id).ended_ts is None
    assert subs.is_alive(PID + 1, START + 1, OTHER)


async def test_new_boot_other_token_untouched(client, db, world, procs, clock):
    """Б1 п.4: новая загрузка снимает строки прежних загрузок только под токеном запроса."""
    other_tok = _token(db, name="mcp:second").token
    theirs = (await _subscribe(client, world, tok=other_tok, boot=BOOT1)).json()["sub"]["id"]
    r = await _subscribe(client, world, pid=PID + 3, start=START + 3, sid="s-3", boot=BOOT2)
    assert r.status_code == 200, r.text
    assert _row(db, theirs).ended_ts is None


async def test_old_boot_without_field_untouched(client, db, world, procs, clock):
    """Подписка старого клиента без поля `boot` по загрузке не снимается — только по К2 (12 ч)."""
    sub_id = (await _subscribe(client, world)).json()["sub"]["id"]
    clock.advance_ms(session_host.STALE_MS + 1_000)
    r = await _subscribe(client, world, pid=PID + 3, start=START + 3, sid="s-3", boot=BOOT2)
    assert r.status_code == 200, r.text
    assert _row(db, sub_id).ended_ts is None
    assert _cleanup(db, clock) == []
    assert session_host.state(LAPTOP, PID, START) == session_host.STALE


async def test_reboot_seen_by_beat_not_by_tokenless_body(client, db, world, procs, clock):
    """Новая загрузка пульсом — подписки прежней сняты сразу; телом без базы и без токена (валидатор) — нет:
    снимет следующий пульс или подписка с этим токеном."""
    a = (await _subscribe(client, world, boot=BOOT1)).json()["sub"]["id"]
    SendSessionIn(**_session(pid=PID + 4, start=START + 4, sid="s-4", boot=BOOT2))
    assert _cleanup(db, clock) == []
    assert _row(db, a).ended_ts is None
    answer = await _beat(client, world, pid=PID + 3, start=START + 3, sid="s-3", boot=BOOT2)
    assert answer == {"remote": True, "subscribed": False, "beat": False, "neighbours": []}
    assert _row(db, a).end_reason == "session_gone"


async def test_reboot_after_core_restart(client, db, world, procs, clock):
    """Рестарт ядра: первый контакт машины с новой загрузкой снимает подписки прежних загрузок из базы."""
    b = (await _subscribe(client, world, pid=PID + 1, start=START + 1, sid="s-1", boot=BOOT2)).json()["sub"][
        "id"
    ]
    _restart(db, clock)
    assert subs.is_alive(PID + 1, START + 1, LAPTOP)
    answer = await _beat(client, world, pid=PID + 5, start=START + 5, sid="s-5", boot=BOOT3)
    assert answer["remote"] is True
    assert _row(db, b).end_reason == "session_gone"


async def test_bad_boot_422(client, db, world, procs):
    r = await client.post(
        "/api/v1/subs/beat", json={"session": _session(boot="не uuid")}, headers=_h(world["tok"])
    )
    assert r.status_code == 422


async def test_get_routes_take_session_host_from_query(client, db, world, procs):
    """19.10: сессия ноутбука при ядре на стационарнике — подписка и ожидание шли, а GET-ручки (входящие
    агентов, напоминания, состояние ролей) судили её по `/proc` ядра: «сессия не найдена». Клиент шлёт `host`
    в параметрах — ручки берут его, как тело `session`."""
    r = await _subscribe(client, world, role="программист")
    assert r.status_code == 200, r.text
    me = {"pid": PID, "proc_start": START, "session_id": SID}
    for path in ("/api/v1/peer-msgs", "/api/v1/reminders", "/api/v1/agents/states"):
        r = await client.get(path, params={**me, "host": LAPTOP}, headers=_h(world["tok"]))
        assert r.status_code == 200, (path, r.text)
        r = await client.get(path, params=me, headers=_h(world["tok"]))  # без машины — `/proc` ядра
        assert r.status_code == 409, (path, r.text)
    r = await client.get("/api/v1/peer-msgs", params={**me, "host": "не-машина"}, headers=_h(world["tok"]))
    assert r.status_code == 422


# --- Ф99-7.3: лента внешней сессии «только смотреть» -----------------------------------------------------


def _tail_event(i, summary="шаг"):
    return {
        "id": i, "ts": 1_000 + i, "kind": "text", "tool": None,
        "summary": summary, "is_error": False, "ref": None,
    }


async def test_beat_tail_saved_and_served_to_ui_only(client, db, world, procs, clock):
    sub_id = (await _subscribe(client, world)).json()["sub"]["id"]
    secret = "API_KEY=sk-abcdefghijklmnop1234567890"
    await _beat(client, world, extra={"tail": [_tail_event(1), _tail_event(2, secret)]})
    r = await client.get(f"/api/v1/subs/{sub_id}/transcript", headers=_h(world["tok"]))
    assert r.status_code == 403  # bearer — нельзя
    async with AsyncClient(transport=ASGITransport(app=client._transport.app), base_url=BASE_URL) as ui:
        await login_via_code(ui, db, clock)
        r = await ui.get(f"/api/v1/subs/{sub_id}/transcript")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["supported"] and body["remote"] and body["refresh_s"] == 60
    assert [e["id"] for e in body["events"]] == [1, 2]
    assert "sk-abcdefghijklmnop1234567890" not in r.text
    # пульс без хвоста прежний хвост не стирает
    await _beat(client, world)
    assert session_host.tail_of(LAPTOP, PID, START)[1] is not None


async def test_beat_tail_validation(client, db, world, procs):
    await _subscribe(client, world)
    bad = {"session": _session(), "tail": [{**_tail_event(1), "kind": "bogus"}]}
    r = await client.post("/api/v1/subs/beat", json=bad, headers=_h(world["tok"]))
    assert r.status_code == 422
    many = {"session": _session(), "tail": [_tail_event(i) for i in range(51)]}
    r = await client.post("/api/v1/subs/beat", json=many, headers=_h(world["tok"]))
    assert r.status_code == 422
