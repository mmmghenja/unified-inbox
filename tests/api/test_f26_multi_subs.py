"""Ф26-1 (docs/architecture.md): несколько сессий с подпиской на
один проект, ядро.

- C1: миграция 0040.
- C2 + П3, П6, П8: `POST /subs` — рядом, предел, `/clear`, роли.
- C3 + П2: наследование по «линии».
- C4: карточки — своя копия каждой подписке `cards`.
- C5: сообщения — каждой подписке `messages` по её фильтру и ключам.
- C6 + П7: подписка на беседу глушит только свою сессию.
- C7: `/agent/items` — `signals`, `msg_signals`.
- C8: `annotate_keys` — объединение ключей.
- C11: сквозной — две сессии, свои сигналы своим ожиданием.

Стиль — как test_f25_sub_keywords.py: публичное приложение через ASGITransport, события через
`ingest.apply_batch`, `/proc` подменён словарём."""

import json

import pytest

from hub.core import chan_subs, ingest, security, subs
from hub.core import db as db_module
from hub.core.db import Db

from .conftest import insert_account, insert_channel, insert_project

ORIGIN = "http://127.0.0.1:8787"
A = (4242, 777_000, "sess-aaaaaaaa-1111")
B = (5151, 888_000, "sess-bbbbbbbb-2222")
C = (6161, 999_000, "sess-cccccccc-3333")
KEYS = {"keywords": ["штраф"], "exclude": ["вакансия"]}
MIN_MS = 60_000


@pytest.fixture
def procs(monkeypatch):
    table = {A[0]: A[1], B[0]: B[1], C[0]: C[1]}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN, "X-Hub-Sub-Wait": "3"}


def _token(db, *, name="mcp:all", kind="interactive", scopes=("read", "draft")):
    return db.write_sync(
        lambda conn: security.create_api_token(
            conn, name=name, kind=kind, scopes=list(scopes), project_ids=None, expires_ts=None
        )
    ).token


@pytest.fixture
def world(db, tmp_path):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    pid = insert_project(db, name="Umbrella", workdir=str(workdir))
    db.write_sync(lambda conn: subs.set_project_subs(conn, pid, True, now_ms=0))
    sl = insert_account(db, source="slack")
    ch = {ext: insert_channel(db, sl, project_id=pid, ext_id=ext, kind="group") for ext in ("C1", "C2")}
    ch["D1"] = insert_channel(db, sl, project_id=pid, ext_id="D1", kind="dm")
    ch["MUTED"] = insert_channel(db, sl, project_id=pid, ext_id="MUTED", kind="group", muted=1)
    return {
        "pid": pid,
        "workdir": str(workdir),
        "acc": sl,
        "ch": ch,
        "tok": _token(db),
        "tok2": _token(db, name="mcp:other"),
        "triage": _token(db, name="agent:triage", kind="background", scopes=("read", "triage")),
    }


def _session(w, who, *, full=True, kind="interactive", name=None):
    pid, start, sid = who
    body = {"pid": pid, "proc_start": start, "session_id": sid}
    if full:
        body.update(
            {"cwd": w["workdir"], "kind": kind, "name": name, "name_source": "user" if name else None}
        )
    return body


async def _sub(client, w, who, *, expect=200, token=None, kind="interactive", name=None, **extra):
    body: dict = {"session": _session(w, who, kind=kind, name=name), **extra}
    if extra.get("mode") == "personal":
        body["mode"], body["msg_filter"] = "messages", "personal"
    r = await client.post("/api/v1/subs", json=body, headers=_h(token or w["tok"]))
    assert r.status_code == expect, r.text
    return r.json()


async def _wait(client, w, sub_id, who):
    r = await client.post(
        f"/api/v1/subs/{sub_id}/wait",
        json={"session": _session(w, who, full=False), "waiter": "0123456789abcdef", "timeout_s": 1},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
    return r.json()["signals"]


async def _chan_sub(client, w, who, channel_id):
    r = await client.post(
        "/api/v1/chan-subs",
        json={"channel_id": channel_id, "session": _session(w, who)},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
    return r.json()["sub"]["id"]


async def _card(client, w, channel_id, message_ids):
    r = await client.post(
        "/api/v1/agent/items",
        json={
            "channel_id": channel_id,
            "message_ids": message_ids,
            "label": "reply",
            "summary": "сводка",
            "action": None,
            "provider": "test",
            "model": "m",
        },
        headers=_h(w["triage"]),
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _ev(chat, ext, ts, *, is_me=False, bot=False, text="x", kind="group"):
    snd: dict = {"ext_id": "U1", "is_me": True} if is_me else {"ext_id": "U2", "name": "Пётр"}
    if bot:
        snd["is_bot"] = True
    return {
        "type": "upsert",
        "channel": {"ext_id": chat, "kind": kind},
        "ext_id": ext,
        "ts": ts,
        "sender": snd,
        "is_me": is_me,
        "kind": "text",
        "text": text,
        "mentions_me": False,
    }


def _ingest(db, w, events, now_ms):
    result = db.write_sync(
        lambda conn: ingest.apply_batch(conn, account_id=w["acc"], events=events, cursor=None, now_ms=now_ms)
    )
    assert result.dead == 0, result


def _now(clock) -> int:
    return int(clock() * 1000)


def _msg_id(db, channel_id, ext):
    schema = db_module.db_of(channel_id)
    with db.reader() as conn:
        return conn.execute(
            f"SELECT id FROM {schema}.message WHERE channel_id = ? AND ext_id = ?",  # noqa: S608
            (channel_id, ext),
        ).fetchone()[0]


def _row(db, sub_id):
    with db.reader() as conn:
        return subs.get_sub(conn, sub_id)


def _card_signals(db) -> list[tuple]:
    with db.reader() as conn:
        return conn.execute(
            "SELECT sub_id, item_id, status, reason FROM main.session_signal ORDER BY id"
        ).fetchall()


def _msg_signals(db) -> list[tuple]:
    """`(chan_sub_id, project_sub_id, channel_id, status, reason, cat, keys)`."""
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT chan_sub_id, project_sub_id, channel_id, status, reason, cat, keys_json "
            "FROM main.session_chan_signal ORDER BY id"
        ).fetchall()
    return [(*r[:6], json.loads(r[6]) if r[6] else None) for r in rows]


def _audit(db, action):
    with db.reader() as conn:
        return [
            json.loads(d)
            for (d,) in conn.execute(
                "SELECT detail_json FROM main.audit WHERE action = ? ORDER BY id", (action,)
            )
        ]


# --- C1: миграция 0040 -----------------------------------------------------------------------------------


_INSERT = (
    "INSERT INTO main.session_sub (project_id, api_token_id, session_pid, session_proc_start, "
    "session_id, session_cwd, created_ts, ended_ts"
)


def _insert_live(conn, *, pid=1, sid="s", role_key=None, ended=None, token=1, old=False):
    if old:  # до 0040 колонок роли нет
        conn.execute(_INSERT + ") VALUES (1, ?, ?, 1, ?, '/', 0, ?)", (token, pid, sid, ended))
        return
    conn.execute(
        _INSERT + ", role, role_key) VALUES (1, ?, ?, 1, ?, '/', 0, ?, ?, ?)",
        (token, pid, sid, ended, role_key, role_key),
    )


def test_c1_migration_0040(tmp_path, monkeypatch):
    original = db_module._migrations
    monkeypatch.setattr(
        db_module, "_migrations", lambda *, main: {v: f for v, f in original(main=main).items() if v <= 39}
    )
    old = Db(tmp_path)
    old.start()
    try:
        old.write_sync(lambda conn: _insert_live(conn, old=True))
        with old.reader() as conn:
            assert conn.execute("PRAGMA main.user_version").fetchone()[0] == 39
            work_version = conn.execute("PRAGMA work.user_version").fetchone()[0]
    finally:
        old.close()
    # Ф28-1: до 0040 включительно — более поздние `_shared` (0041) законно двигают версию work.
    monkeypatch.setattr(
        db_module, "_migrations", lambda *, main: {v: f for v, f in original(main=main).items() if v <= 40}
    )
    for _ in range(2):  # повторный запуск — без ошибок
        new = Db(tmp_path)
        new.start()
        try:
            with new.reader() as conn:
                assert conn.execute("PRAGMA main.user_version").fetchone()[0] >= 40
                assert (
                    conn.execute("PRAGMA work.user_version").fetchone()[0] == work_version
                )  # work не тронут
                indexes = {
                    r[0] for r in conn.execute("SELECT name FROM main.sqlite_master WHERE type = 'index'")
                }
                assert "session_sub_live_project" not in indexes
                assert {
                    "session_sub_live_session",
                    "session_sub_live_project_all",
                    "session_sub_live_role",
                } <= (indexes)
                cols = {r[1] for r in conn.execute("PRAGMA main.table_info(session_sub)")}
                assert {"role", "role_key"} <= cols
                [(role, role_key)] = conn.execute("SELECT role, role_key FROM main.session_sub").fetchall()
                assert (role, role_key) == (None, None)  # прежняя строка цела
        finally:
            new.close()
    new = Db(tmp_path)
    new.start()
    try:
        # две живые подписки разных сессий — вставляются; две живые одной сессии — нет
        new.write_sync(lambda conn: _insert_live(conn, pid=2, sid="s2"))
        with pytest.raises(Exception, match="UNIQUE"):
            new.write_sync(lambda conn: _insert_live(conn, pid=2, sid="s2"))
        # та же сессия другим токеном — отдельная строка (ядро её заменяет, П3)
        new.write_sync(lambda conn: _insert_live(conn, pid=2, sid="s2", token=2))
        # одна живая на роль в проекте; завершённые с той же ролью — сколько угодно
        new.write_sync(lambda conn: _insert_live(conn, pid=3, sid="s3", role_key="маркетолог"))
        with pytest.raises(Exception, match="UNIQUE"):
            new.write_sync(lambda conn: _insert_live(conn, pid=4, sid="s4", role_key="маркетолог"))
        new.write_sync(lambda conn: _insert_live(conn, pid=5, sid="s5", role_key="маркетолог", ended=7))
        new.write_sync(lambda conn: _insert_live(conn, pid=6, sid="s6", role_key="маркетолог", ended=8))
    finally:
        new.close()


# --- C2: POST /subs --------------------------------------------------------------------------------------


async def test_c2_second_session_side_by_side_and_limit(client, db, world, procs):
    first = (await _sub(client, world, A))["sub"]
    body = await _sub(client, world, B)
    assert body["created"] is True
    assert body["neighbours"] == [
        {
            "id": first["id"],
            "role": None,
            "role_no": 1,  # Ф35-1
            "session_name": None,
            "mode": "cards",
            "msg_filter": "all",
            "keys": None,
            "accounts": None,  # Ф38-1
        }
    ]
    assert _row(db, first["id"]).ended_ts is None
    ids = [first["id"], body["sub"]["id"]]
    for n in range(3):
        procs[7000 + n] = 1
        ids.append((await _sub(client, world, (7000 + n, 1, f"sess-{n}")))["sub"]["id"])
    procs[8000] = 1
    r = await _sub(client, world, (8000, 1, "sess-sixth"), expect=409)
    assert r["detail"] == "в проекте уже 5 подписок: " + ", ".join(f"#{i}" for i in ids)
    # мёртвая освобождает место
    del procs[7000]
    body = await _sub(client, world, (8000, 1, "sess-sixth"))
    assert body["created"] is True and _row(db, ids[2]).end_reason == "session_gone"


async def test_c2_clear_ends_only_own(client, db, world, procs):
    a = (await _sub(client, world, A))["sub"]["id"]
    b = (await _sub(client, world, B))["sub"]["id"]
    body = await _sub(client, world, (A[0], A[1], "sess-after-clear"))
    assert body["created"] is True
    assert _row(db, a).end_reason == "conversation_changed"
    assert _row(db, b).ended_ts is None


async def test_c2_dead_same_role_replaced_without_role_gone(client, db, world, procs):
    a = (await _sub(client, world, A, role="Маркетолог"))["sub"]
    assert a["role"] == "Маркетолог" and _row(db, a["id"]).role_key == "маркетолог"
    b = (await _sub(client, world, B))["sub"]["id"]
    del procs[A[0]], procs[B[0]]
    body = await _sub(client, world, C, role="маркетолог")  # регистр не важен
    assert body["sub"]["role"] == "маркетолог"
    assert _row(db, a["id"]).end_reason == "replaced"
    assert _row(db, b).end_reason == "session_gone"


async def test_c2_live_foreign_role_409_and_yo(client, db, world, procs):
    """Ф35-1 (решение 2): вторая живая сессия роли — не 409, а «роль 2»; 409 — только на занятый номер."""
    a = (await _sub(client, world, A, role="Ёлка"))["sub"]["id"]
    r = await _sub(client, world, B, role="елка", number=1, expect=409)
    assert r["detail"] == f"роль «Ёлка» №1 уже слушает сессия #{a}"
    assert _row(db, a).ended_ts is None
    b = (await _sub(client, world, B, role="елка"))["sub"]
    assert (b["role"], b["role_no"]) == ("елка", 2) and _row(db, a).ended_ts is None


async def test_c2_role_change_in_place_and_remove(client, db, world, procs):
    a = (await _sub(client, world, A))["sub"]
    body = await _sub(client, world, A, role="программист")
    assert body["created"] is False and body["sub"]["id"] == a["id"] and body["sub"]["role"] == "программист"
    assert _audit(db, "sub_mode")[-1] == {"sub_id": a["id"], "role": True}
    # повтор с той же ролью — ничего не пишется
    await _sub(client, world, A, role="программист")
    assert len(_audit(db, "sub_mode")) == 1
    # роль другой живой сессии: занятый номер — 409, своя не меняется (Ф35-1: без номера стала бы №2)
    await _sub(client, world, B, role="маркетолог")
    r = await _sub(client, world, A, role="маркетолог", number=1, expect=409)
    assert "уже слушает сессия" in r["detail"]
    assert _row(db, a["id"]).role == "программист"
    # role="" — снять
    body = await _sub(client, world, A, role="")
    assert body["sub"]["role"] is None and _row(db, a["id"]).role_key is None
    assert _audit(db, "sub_mode")[-1] == {"sub_id": a["id"], "role": False}
    # без role в запросе — роль не трогается
    await _sub(client, world, A, role="тестировщик")
    body = await _sub(client, world, A, mode="cards")
    assert body["sub"]["role"] == "тестировщик"


async def test_c2_p8_role_held_by_dead_is_replaced_on_change(client, db, world, procs):
    a = (await _sub(client, world, A, role="маркетолог"))["sub"]["id"]
    b = (await _sub(client, world, B))["sub"]["id"]
    del procs[A[0]]
    body = await _sub(client, world, B, role="маркетолог")
    assert body["sub"]["id"] == b and body["sub"]["role"] == "маркетолог"
    assert _row(db, a).end_reason == "replaced"


@pytest.mark.parametrize("bad", ["a" * 33, "роль;drop", "роль\nвторая", "@марк"])
async def test_c2_bad_role_422(client, db, world, procs, bad):
    r = await _sub(client, world, A, role=bad, expect=422)
    assert "role: 1–32 символа" in r["detail"]


async def test_c2_p3_same_session_other_token_replaced(client, db, world, procs):
    a = (await _sub(client, world, A))["sub"]["id"]
    body = await _sub(client, world, A, token=world["tok2"])
    assert body["created"] is True
    assert _row(db, a).end_reason == "replaced"
    with db.reader() as conn:
        live = conn.execute("SELECT COUNT(*) FROM main.session_sub WHERE ended_ts IS NULL").fetchone()[0]
    assert live == 1


async def test_c2_p6_stale_opencode_sub_is_dead(client, db, world, procs, clock):
    oc = (A[0], A[1], "ses_AAAAAAAAAAAAAAAAAAAAAAAAAA")
    a = (await _sub(client, world, oc, kind="opencode", role="маркетолог"))["sub"]["id"]
    clock.advance_ms(11 * MIN_MS)  # ожидания не было 11 мин — брошена (П1 Ф16-4)
    body = await _sub(client, world, B, role="маркетолог")
    assert body["created"] is True
    assert _row(db, a).end_reason == "replaced"


async def test_c2_p8_integrity_error_is_409(db, world, procs, monkeypatch):
    """П8: гонка с уникальным индексом (подписка этой сессии появилась между чтением и вставкой) — 409."""
    real = subs.live_subs
    monkeypatch.setattr(subs, "live_subs", lambda conn, project_id: [])
    db.write_sync(lambda conn: _insert_live_for(conn, world, A))

    class P:
        kind = "interactive"
        token_id = 1
        project_ids = None

    def _fn(conn):
        return subs.subscribe(
            conn,
            principal=P(),
            session=_session(world, A),
            project_id=None,
            now_ms=1,
            client_name=None,
            request_id="r",
        )

    with pytest.raises(subs.SubError) as exc:
        db.write_sync(_fn)
    assert exc.value.status == 409
    monkeypatch.setattr(subs, "live_subs", real)


def _insert_live_for(conn, w, who):
    conn.execute(
        "INSERT INTO main.session_sub (project_id, api_token_id, session_pid, session_proc_start, "
        "session_id, session_cwd, created_ts) VALUES (?, 1, ?, ?, ?, '/', 0)",
        (w["pid"], *who),
    )


# --- C3 + П2: наследование -------------------------------------------------------------------------------


async def test_c3_inherit_by_role_after_restart(client, db, world, procs):
    first = (await _sub(client, world, A, mode="personal", role="маркетолог", **KEYS))["sub"]
    await _sub(client, world, B, mode="messages", msg_filter="all")  # сосед жив
    del procs[A[0]]
    body = await _sub(client, world, C, role="Маркетолог")  # новый pid и session_id
    sub = body["sub"]
    assert (body["inherited_from"], body["inherited_why"]) == (first["id"], "role")
    assert (sub["mode"], sub["msg_filter"], sub["keywords"]) == ("messages", "personal", first["keywords"])
    assert _audit(db, "sub_create")[-1]["inherited_why"] == "role"
    assert _audit(db, "sub_create")[-1]["role"] is True


async def test_c3_role_given_without_own_line_inherits_nothing(client, db, world, procs):
    """П2.1: задана роль — только подписки с той же ролью; ключи безымянной соседки не берутся."""
    await _sub(client, world, A, mode="personal", **KEYS)
    del procs[A[0]]
    body = await _sub(client, world, C, role="программист")
    assert "inherited_from" not in body and body["sub"]["mode"] == "cards"


async def test_c3_inherit_by_process_after_clear_with_live_neighbour(client, db, world, procs):
    first = (await _sub(client, world, A, mode="personal", role="маркетолог", **KEYS))["sub"]
    await _sub(client, world, B, mode="messages", msg_filter="all")
    body = await _sub(client, world, (A[0], A[1], "sess-after-clear"))
    sub = body["sub"]
    assert (body["inherited_from"], body["inherited_why"]) == (first["id"], "process")
    assert (sub["mode"], sub["msg_filter"], sub["keywords"], sub["role"]) == (
        "messages",
        "personal",
        first["keywords"],
        "маркетолог",
    )


async def test_c3_inherit_by_session_id(client, db, world, procs):
    """Возобновлённая сессия (тот же `session_id`, новый процесс)."""
    first = (await _sub(client, world, A, mode="messages", msg_filter="all", role="руководитель"))["sub"]
    await _sub(client, world, B)
    del procs[A[0]]
    body = await _sub(client, world, (C[0], C[1], A[2]))
    assert (body["inherited_from"], body["inherited_why"]) == (first["id"], "session")
    assert (body["sub"]["mode"], body["sub"]["role"]) == ("messages", "руководитель")
    assert _row(db, first["id"]).end_reason == "replaced"  # тот же session_id у мёртвой — `replaced`


async def test_c3_inherit_by_name_without_role(client, db, world, procs):
    first = (await _sub(client, world, A, mode="personal", role="маркетолог", name="МАРКЕТОЛОГ", **KEYS))[
        "sub"
    ]
    await _sub(client, world, B)
    del procs[A[0]]
    body = await _sub(client, world, C, name="МАРКЕТОЛОГ")
    sub = body["sub"]
    assert (body["inherited_from"], body["inherited_why"]) == (first["id"], "name")
    assert sub["keywords"] == first["keywords"] and sub["role"] is None  # роль по имени не наследуется


async def test_c3_only_sub_in_project_as_before(client, db, world, procs):
    first = (await _sub(client, world, A, mode="messages", msg_filter="all"))["sub"]
    del procs[A[0]]
    body = await _sub(client, world, C)
    assert (body["inherited_from"], body["inherited_why"]) == (first["id"], "only")
    assert body["sub"]["mode"] == "messages"


async def test_c3_live_neighbour_no_line_nothing_inherited(client, db, world, procs):
    """Ключи соседа (живого) и завершённой чужой подписки при живом соседе не наследуются."""
    await _sub(client, world, A, mode="personal", **KEYS)
    await _sub(client, world, B, mode="personal", keywords=["другое"])
    del procs[A[0]]
    body = await _sub(client, world, C)
    assert "inherited_from" not in body and body["sub"]["mode"] == "cards"
    assert [n["id"] for n in body["neighbours"]] == [2]
    assert body["neighbours"][0]["keys"] == {"keys": 1, "exclude": 0}


async def test_c3_p2_reboot_two_roles(client, db, world, procs):
    """П2: перезагрузка — обе роли мертвы; первым поднимается программист без роли — ни ключей, ни роли
    маркетолога; маркетолог с `role` — свои."""
    mk = (await _sub(client, world, A, mode="personal", role="маркетолог", **KEYS))["sub"]
    await _sub(client, world, B, role="программист")
    del procs[A[0]], procs[B[0]]
    body = await _sub(client, world, C)  # программист без роли: в проекте нет живых, но были роли за сутки
    assert "inherited_from" not in body
    assert (body["sub"]["mode"], body["sub"]["keywords"], body["sub"]["role"]) == ("cards", None, None)
    procs[9000] = 1
    body = await _sub(client, world, (9000, 1, "sess-mk"), role="маркетолог")
    assert (body["inherited_from"], body["inherited_why"]) == (mk["id"], "role")
    assert body["sub"]["keywords"] == mk["keywords"]


async def test_c3_p2_two_opencode_sessions_same_pid_no_cross_inherit(client, db, world, procs):
    oc_a = (A[0], A[1], "ses_AAAAAAAAAAAAAAAAAAAAAAAAAA")
    oc_b = (A[0], A[1], "ses_BBBBBBBBBBBBBBBBBBBBBBBBBB")
    await _sub(client, world, oc_a, kind="opencode", mode="personal", **KEYS)
    body = await _sub(client, world, oc_b, kind="opencode")
    assert body["created"] is True and "inherited_from" not in body and body["sub"]["mode"] == "cards"
    with db.reader() as conn:
        live = conn.execute("SELECT COUNT(*) FROM main.session_sub WHERE ended_ts IS NULL").fetchone()[0]
    assert live == 2


async def test_c3_role_inherited_not_when_taken(client, db, world, procs):
    """Ф35-1 П1: прежнюю линию сессии держит живая чужая — не тихий переезд (и не подписка без роли), а
    409 «задай number явно»; с номером — роль по процессу, режим — только из своей линии (её ещё не было)."""
    first = (await _sub(client, world, A, mode="messages", msg_filter="all", role="маркетолог"))["sub"]["id"]
    r = await client.post(
        f"/api/v1/subs/{first}/end",
        json={"session": _session(world, A, full=False), "reason": "unsubscribed"},
        headers=_h(world["tok"]),
    )
    assert r.status_code == 200, r.text
    taken = (await _sub(client, world, B, role="маркетолог"))["sub"]["id"]  # роль свободна — B её берёт
    r = await _sub(client, world, (A[0], A[1], "sess-new"), expect=409)  # /clear в процессе A
    assert r["detail"] == f"твою линию «маркетолог» №1 держит сессия #{taken} — задай number явно"
    body = await _sub(client, world, (A[0], A[1], "sess-new"), number=2)
    assert (body["inherited_from"], body["inherited_why"]) == (first, "process")
    assert (body["sub"]["mode"], body["sub"]["role"], body["sub"]["role_no"]) == ("cards", "маркетолог", 2)


async def _end(client, w, sub_id, who):
    r = await client.post(
        f"/api/v1/subs/{sub_id}/end",
        json={"session": _session(w, who, full=False), "reason": "unsubscribed"},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text


async def test_r11_reboot_two_roleless_sessions_no_swap(client, db, world, procs, clock):
    """Р11 (ревью, `.runs/critic-f26/`): две безымянные сессии жили рядом (карточки и «лично мне» с ключами),
    перезагрузка — первая поднявшаяся не забирает режим и ключи соседки; вторая тоже ничего не наследует."""
    await _sub(client, world, A)
    await _sub(client, world, B, mode="personal", **KEYS)
    clock.advance_ms(MIN_MS)  # жили рядом
    procs.clear()
    procs[7001], procs[7002] = 1, 2
    body = await _sub(client, world, (7001, 1, "sess-new-cards"))
    assert "inherited_from" not in body and body["inherit_blocked"] == "several_roleless"
    assert (body["sub"]["mode"], body["sub"]["keywords"]) == ("cards", None)
    body2 = await _sub(client, world, (7002, 2, "sess-new-mk"))
    assert "inherited_from" not in body2 and body2["sub"]["keywords"] is None


async def test_r11_neighbour_closed_first_then_restart_takes_nothing(client, db, world, procs, clock):
    await _sub(client, world, A)
    b = (await _sub(client, world, B, mode="personal", **KEYS))["sub"]["id"]
    clock.advance_ms(MIN_MS)
    await _end(client, world, b, B)  # маркетолог закрыт раньше
    clock.advance_ms(MIN_MS)
    del procs[A[0]]  # «карточная» перезапущена (новый процесс, новый разговор)
    body = await _sub(client, world, C)
    assert "inherited_from" not in body and body["sub"]["keywords"] is None


async def test_r11_one_roleless_session_sequential_restarts_inherits(client, db, world, procs, clock):
    """Одна безымянная сессия, перезапускавшаяся последовательно (с /clear в середине), — наследует как
    раньше: цепочка, где старая завершена в момент создания новой, — не пересечение."""
    first = (await _sub(client, world, A, mode="personal", **KEYS))["sub"]
    clock.advance_ms(MIN_MS)
    second = await _sub(client, world, (A[0], A[1], "sess-after-clear"))
    assert second["inherited_why"] == "process"
    clock.advance_ms(MIN_MS)
    del procs[A[0]]
    body = await _sub(client, world, C)
    assert (body["inherited_from"], body["inherited_why"]) == (second["sub"]["id"], "only")
    assert body["sub"]["keywords"] == first["keywords"]
    clock.advance_ms(MIN_MS)
    del procs[C[0]]
    procs[7003] = 3
    body = await _sub(client, world, (7003, 3, "sess-after-reboot"))
    assert body["inherited_why"] == "only" and body["sub"]["keywords"] == first["keywords"]


async def test_r11_latest_by_ended_ts(client, db, world, procs, clock):
    """«Самая свежая» — по моменту завершения: подписка с меньшим номером, завершённая позже, — источник."""
    early = (await _sub(client, world, A, mode="personal", name="МАРКЕТОЛОГ", **KEYS))["sub"]
    late = (await _sub(client, world, B, mode="messages", msg_filter="all", name="МАРКЕТОЛОГ"))["sub"]
    await _end(client, world, late["id"], B)
    clock.advance_ms(MIN_MS)
    await _end(client, world, early["id"], A)
    body = await _sub(client, world, C, name="МАРКЕТОЛОГ")
    assert (body["inherited_from"], body["inherited_why"]) == (early["id"], "name")
    assert body["sub"]["keywords"] == early["keywords"]


# --- C4: карточки ----------------------------------------------------------------------------------------


async def test_c4_two_cards_subs_two_signals(client, db, world, procs, clock):
    now = _now(clock)
    _ingest(db, world, [_ev("C1", "1.0", now)], now)
    a = (await _sub(client, world, A))["sub"]["id"]
    b = (await _sub(client, world, B))["sub"]["id"]
    item = await _card(client, world, world["ch"]["C1"], [_msg_id(db, world["ch"]["C1"], "1.0")])
    assert _card_signals(db) == [(a, item, "pending", None), (b, item, "pending", None)]
    got = await _wait(client, world, a, A)
    assert [s["item_id"] for s in got] == [item]
    assert _card_signals(db) == [(a, item, "delivered", None), (b, item, "pending", None)]
    # Владелец ответил — у второй снимается при её выдаче
    _ingest(db, world, [_ev("C1", "2.0", now + 1, is_me=True)], now)
    assert await _wait(client, world, b, B) == []
    assert _card_signals(db)[-1] == (b, item, "skipped", "answered")


async def test_c4_backfill_of_second_sub(client, db, world, procs, clock):
    now = _now(clock)
    _ingest(db, world, [_ev("C1", "1.0", now)], now)
    a = (await _sub(client, world, A))["sub"]["id"]
    item = await _card(client, world, world["ch"]["C1"], [_msg_id(db, world["ch"]["C1"], "1.0")])
    body = await _sub(client, world, B)
    assert body["backfilled"] == 1
    assert _card_signals(db) == [(a, item, "pending", None), (body["sub"]["id"], item, "pending", None)]


# --- C5: сообщения ---------------------------------------------------------------------------------------


async def test_c5_messages_all_and_personal_keys(client, db, world, procs, clock):
    a = (await _sub(client, world, A, mode="messages", msg_filter="all"))["sub"]["id"]
    b = (await _sub(client, world, B, mode="personal", **KEYS))["sub"]["id"]
    now = _now(clock)
    c1, c2, muted = world["ch"]["C1"], world["ch"]["C2"], world["ch"]["MUTED"]
    _ingest(db, world, [_ev("C1", "1.0", now, text="штраф пришёл")], now)
    assert _msg_signals(db) == [
        (None, a, c1, "pending", None, "other", None),
        (None, b, c1, "pending", None, "keyword", ["штраф"]),
    ]
    _ingest(db, world, [_ev("C2", "1.0", now + 1, text="ни о чём")], now)
    assert _msg_signals(db)[2:] == [
        (None, a, c2, "pending", None, "other", None),
        (None, b, c2, "skipped", "no_keyword", None, None),
    ]
    # заглушённая беседа — только подписке с ключами
    _ingest(db, world, [_ev("MUTED", "1.0", now + 2, text="штраф")], now)
    assert _msg_signals(db)[4:] == [(None, b, muted, "pending", None, "keyword", ["штраф"])]
    # бот — `skipped` у обеих
    _ingest(db, world, [_ev("C2", "2.0", now + 3, bot=True, text="штраф")], now)
    assert _msg_signals(db)[5:] == [
        (None, a, c2, "skipped", "bot", None, None),
        (None, b, c2, "skipped", "bot", None, None),
    ]


async def test_c5_channel_migrate_moves_both(client, db, world, procs, clock):
    a = (await _sub(client, world, A, mode="messages", msg_filter="all"))["sub"]["id"]
    b = (await _sub(client, world, B, mode="messages", msg_filter="all"))["sub"]["id"]
    now = _now(clock)
    _ingest(db, world, [_ev("C1", "1.0", now)], now)
    _ingest(
        db,
        world,
        [{"type": "channel_migrate", "old_ext_id": "C1", "channel": {"ext_id": "C1NEW", "kind": "group"}}],
        now,
    )
    with db.reader() as conn:
        new_id = conn.execute("SELECT id FROM main.channel WHERE ext_id = 'C1NEW'").fetchone()[0]
    assert [(r[1], r[2], r[3]) for r in _msg_signals(db)] == [(a, new_id, "pending"), (b, new_id, "pending")]


# --- C6 + П7: подписка на беседу — только своя сессия ------------------------------------------------------


async def test_c6_chan_sub_silences_only_own_session_messages(client, db, world, procs, clock):
    c1 = world["ch"]["C1"]
    a = (await _sub(client, world, A, mode="messages", msg_filter="all"))["sub"]["id"]
    b = (await _sub(client, world, B, mode="messages", msg_filter="all"))["sub"]["id"]
    chan = await _chan_sub(client, world, A, c1)
    now = _now(clock)
    _ingest(db, world, [_ev("C1", "1.0", now)], now)
    assert [(r[0], r[1], r[3]) for r in _msg_signals(db)] == [(chan, None, "pending"), (None, b, "pending")]
    assert a not in {r[1] for r in _msg_signals(db)}


async def test_c6_p7_old_message_for_chan_sub_still_reaches_other_session(client, db, world, procs, clock):
    """П7: сообщение старше подписки на беседу (окно 10 мин) — подписка на беседу молчит, а подписка на проект
    другой сессии (старше сообщения) его получает."""
    now = _now(clock)
    b = (await _sub(client, world, B, mode="messages", msg_filter="all"))["sub"]["id"]
    clock.advance_ms(30 * MIN_MS)
    await _chan_sub(client, world, A, world["ch"]["C1"])
    _ingest(db, world, [_ev("C1", "1.0", now + 5 * MIN_MS)], _now(clock))
    assert [(r[0], r[1], r[3]) for r in _msg_signals(db)] == [(None, b, "pending")]


async def test_c6_deliver_messages_skips_chan_sub_only_for_own_session(client, db, world, procs, clock):
    c1 = world["ch"]["C1"]
    a = (await _sub(client, world, A, mode="messages", msg_filter="all"))["sub"]["id"]
    b = (await _sub(client, world, B, mode="messages", msg_filter="all"))["sub"]["id"]
    now = _now(clock)
    _ingest(db, world, [_ev("C1", "1.0", now)], now)  # оба `pending`
    await _chan_sub(client, world, A, c1)
    assert await _wait(client, world, a, A) == []
    assert [s["channel_id"] for s in await _wait(client, world, b, B)] == [c1]
    rows = {r[1]: (r[3], r[4]) for r in _msg_signals(db) if r[1] is not None}
    assert rows == {a: ("skipped", "chan_sub"), b: ("delivered", None)}


# --- C7: /agent/items -----------------------------------------------------------------------------------


async def test_c7_agent_items_signals_per_sub(client, db, world, procs, clock):
    now = _now(clock)
    c1 = world["ch"]["C1"]
    a = (await _sub(client, world, A))["sub"]["id"]
    b = (await _sub(client, world, B))["sub"]["id"]
    m = (await _sub(client, world, C, mode="messages", msg_filter="all"))["sub"]["id"]
    procs[A[0] + 1] = 1
    p = (await _sub(client, world, (A[0] + 1, 1, "sess-p"), mode="personal", keywords=["штраф"]))["sub"]["id"]
    _ingest(db, world, [_ev("C1", "1.0", now, text="ни о чём")], now)
    item = await _card(client, world, c1, [_msg_id(db, c1, "1.0")])
    await _wait(client, world, a, A)
    r = await client.get("/api/v1/agent/items", params={"project": world["pid"]}, headers=_h(world["triage"]))
    assert r.status_code == 200, r.text
    [card] = [c for c in r.json() if c["id"] == item]
    assert [(s["sub_id"], s["status"]) for s in card["signals"]] == [(a, "delivered"), (b, "pending")]
    assert card["signal"]["sub_id"] == b  # старое поле — последний по номеру сигнала
    assert [(s["sub_ref"], s["status"], s["reason"]) for s in card["msg_signals"]] == [
        (str(m), "pending", None),
        (str(p), "skipped", "no_keyword"),
    ]
    assert card["msg_signal"] is not None  # старое поле на месте


# --- C8: annotate_keys ---------------------------------------------------------------------------------


async def test_c8_annotate_keys_union(client, db, world, procs, clock):
    await _sub(client, world, A, mode="personal", keywords=["штраф", "бан"])
    await _sub(client, world, B, mode="personal", keywords=["выплат", "штраф"], exclude=["вакансия"])
    msgs = [
        {"id": 1, "is_me": False, "text": "бан и штраф и выплата"},
        {"id": 2, "is_me": False, "text": "вакансия: выплата"},  # минус-слово только у B — ключи A остаются
        {"id": 3, "is_me": False, "text": "вакансия: бан"},
        {"id": 4, "is_me": True, "text": "штраф"},
    ]
    with db.reader() as conn:
        chan_subs.annotate_keys(conn, world["ch"]["C1"], msgs)
    assert msgs[0]["keys"] == ["штраф", "бан", "выплат"]
    assert "keys" not in msgs[1]
    assert msgs[2]["keys"] == ["бан"]
    assert "keys" not in msgs[3]


# --- C11: сквозной ---------------------------------------------------------------------------------------


async def test_c11_two_sessions_each_own_signal(client, db, world, procs, clock):
    c1 = world["ch"]["C1"]
    now = _now(clock)
    a = (await _sub(client, world, A, role="руководитель"))["sub"]["id"]
    # Ф36-1: `batch_min=0` — сигнал «по ключу» сразу, без пачки
    b = (await _sub(client, world, B, mode="personal", role="маркетолог", batch_min=0, **KEYS))["sub"]["id"]
    _ingest(db, world, [_ev("C1", "1.0", now, text="у нас штраф")], now)
    item = await _card(client, world, c1, [_msg_id(db, c1, "1.0")])
    got_b = await _wait(client, world, b, B)
    assert [(s["kind"], s["channel_id"], s["cat"], s["keys"]) for s in got_b] == [
        ("messages", c1, "keyword", ["штраф"])
    ]
    got_a = await _wait(client, world, a, A)
    assert [(s["item_id"], s["channel_id"]) for s in got_a] == [(item, c1)]
    # ни одна не забрала чужой: повторные ожидания пусты, сигналы выданы каждый своей подписке
    assert await _wait(client, world, a, A) == []
    assert _card_signals(db) == [(a, item, "delivered", None)]
    assert [(r[1], r[3]) for r in _msg_signals(db)] == [(b, "delivered")]
