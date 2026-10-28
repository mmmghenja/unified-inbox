"""Ф66-1 (docs/architecture.md–D, G; поправки П1–П15): таблица маршрута
карточек проекта.

- перенос прежних настроек на старте ядра (`card_route.migrate`): делегат, «головная закрывает проект»,
  открытая сессия с каналом, выключатель `human_agent` (ключа в каталоге уже нет — читается сырым),
  `agent_auto`; идемпотентность, заданная таблица не трогается, `auto` не ставится никогда;
- маршрут карточки: ручная сессия хаба и проект 0 — `me`, виды без «да» при `auto` — `agent`;
- запись: кривое — 422, кука с ноутбука и с телефона — да, bearer — 403; bearer таблицу не видит;
- «сразу да» по группам тем же кодом, что клик владельца: история `closed_by='system'`, аудит
  `card_route_auto`; пределы и страховка держат карточку владельцу; сессия видит то же, что до клика.

Мир и помощники — test_hub_sessions.py, test_f56_queue.py; хост поддельный."""

import json

from httpx import ASGITransport, AsyncClient

from hub import autopilot as catalog
from hub.core import autosend, bot_guard, card_route, held, held_signals, held_work, presses
from hub.core import join as join_mod
from hub.core import remote as remote_mod
from hub.core.app_public import create_public_app
from hub.core.feed import Notifier
from hub.core.remote import RemoteState

from . import test_hub_sessions as base
from .conftest import PORT, csrf_headers, login_via_code
from .test_f41_autopilot import PASSWORD, REMOTE_HOST, REMOTE_ORIGIN, REMOTE_PORT
from .test_f41_autopilot_5b import _auto, _channel, _me, _send
from .test_f41_held_work import _ask
from .test_f44_presses import _bot_msg, _ident, _press
from .test_f56_queue import _iso, _outbox, _outcomes, _queue, _refs
from .test_hub_sessions import A, _audits, _h, _mk, _now, _sub

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"
ALL_ME = dict.fromkeys(card_route.GROUPS, "me")


# --- помощники ----------------------------------------------------------------------------------------------


async def _route(ui, w, changes: dict, *, project="p3", expect=200):
    r = await ui.c.patch(f"{API}/projects/{w[project]}", json={"card_route": changes}, headers=ui.headers)
    assert r.status_code == expect, r.text
    return r


def _policy(db, pid) -> dict:
    with db.reader() as conn:
        raw = conn.execute("SELECT agent_policy_json FROM main.project WHERE id = ?", (pid,)).fetchone()[0]
    return json.loads(raw) if raw else {}


def _set_policy(db, pid, **keys) -> None:
    """Политика проекта «как до Ф66-1»: без `card_route`, с прежними ключами."""
    policy = {k: v for k, v in _policy(db, pid).items() if k != card_route.POLICY_KEY}
    policy.update(keys)
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.project SET agent_policy_json = ? WHERE id = ?", (json.dumps(policy), pid)
        )
    )


def _global_hold(db, hold: dict) -> None:
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO main.meta (key, value) VALUES ('autopilot_hold', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps(hold),),
        )
    )


def _migrate(db, clock) -> list[int]:
    now = _now(clock)
    return db.write_sync(lambda conn: card_route.migrate(conn, now_ms=now))


def _table(db, pid) -> dict:
    with db.reader() as conn:
        return card_route.table(conn, pid)


def _with(**groups) -> dict:
    return {**ALL_ME, **groups}


def _system_items(db) -> list[tuple]:
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT ref, kind, closed_by, payload_json FROM main.owner_item WHERE closed_by = 'system' "
            "ORDER BY id"
        ).fetchall()
    return [(ref, kind, by, json.loads(payload)) for ref, kind, by, payload in rows]


def _auto_audits(db, result=None) -> list[dict]:
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT result, detail_json FROM main.audit WHERE action = ? ORDER BY id",
            (card_route.AUDIT_ACTION,),
        ).fetchall()
    return [json.loads(d) | {"result": r} for r, d in rows if result is None or r == result]


def _sweep(db, clock) -> list[str]:
    now = _now(clock)
    return db.write_sync(lambda conn: card_route.sweep(conn, now_ms=now))


# --- перенос (A.4, П1, П7, П8) ------------------------------------------------------------------------------


async def test_migrate_delegate_human_switch_and_head_channel(client, ui, db, clock, world):
    w = world
    assert "human_agent" not in catalog.KEYS  # П1: ключа в каталоге нет — перенос читает его сырым
    # p3: назначенный делегат Ф49, `human_agent` стоит (по умолчанию) -> разрешения и вопросы агенту.
    _set_policy(db, w["p3"], card_delegate={"sub_id": 7, "role": "оркестратор"})
    # p4: `human_agent` снят для проекта, канала нет -> агенту только «нужен человек».
    _set_policy(db, w["p4"], autopilot_hold={"human_agent": False})
    # off: то же, но `agent_auto.human = false` -> всё владельцу.
    _set_policy(db, w["off"], autopilot_hold={"human_agent": False}, agent_auto={"human": False})
    # nodir: «головная закрывает проект» и снятый `human_agent` -> всё агенту, кроме выключенного `draft`.
    _set_policy(
        db,
        w["nodir"],
        card_delegate={"head_channel": True},
        autopilot_hold={"human_agent": False},
        agent_auto={"draft": False},
    )
    moved = _migrate(db, clock)
    assert sorted(moved) == sorted([w["p3"], w["p4"], w["off"], w["nodir"]])
    assert _table(db, w["p3"]) == _with(permission="agent", question="agent")
    assert _table(db, w["p4"]) == _with(human="agent")
    assert _table(db, w["off"]) == ALL_ME
    # Ф93-1: строку «Деньги» перенос не ставит — `me`.
    assert _table(db, w["nodir"]) == {
        **dict.fromkeys(card_route.GROUPS, "agent"),
        "draft": "me",
        "money": "me",
    }
    # П8: прежние ключи остаются; `auto` не ставится никогда.
    assert _policy(db, w["nodir"])["card_delegate"] == {"head_channel": True}
    assert all("auto" not in _table(db, pid).values() for pid in moved)
    audits = _audits(db, "card_route_migrate")
    assert sorted(a["project_id"] for a in audits) == sorted(moved)


async def test_migrate_head_without_human_switch_is_me(client, ui, db, clock, world):
    """Канал включался только при снятом `human_agent`: «головная» при стоящем выключателе — всё владельцу; снятый
    глобально и поставленный в проекте — проект важнее."""
    w = world
    _global_hold(db, {"human_agent": False})
    _set_policy(db, w["p3"], card_delegate={"head_channel": True}, autopilot_hold={"human_agent": True})
    _set_policy(db, w["p4"], card_delegate={"head_channel": True})
    _migrate(db, clock)
    assert _table(db, w["p3"]) == ALL_ME
    assert _table(db, w["p4"]) == {**dict.fromkeys(card_route.GROUPS, "agent"), "money": "me"}


async def test_migrate_open_via_agent_session_counts_closed_does_not(client, ui, db, clock, world):
    w = world
    _global_hold(db, {"human_agent": False})
    open_sid = (await _mk(ui, w, project="p3"))["id"]
    closed_sid = (await _mk(ui, w, project="p4"))["id"]
    now = _now(clock)
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.hub_session SET via_agent = 1 WHERE id IN (?, ?)", (open_sid, closed_sid)
        )
    )
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.hub_session SET closed_ts = ? WHERE id = ?", (now, closed_sid))
    )
    _set_policy(db, w["p3"], agent_auto={"permission": False})
    _set_policy(db, w["p4"])
    _migrate(db, clock)
    assert _table(db, w["p3"]) == {
        **dict.fromkeys(card_route.GROUPS, "agent"),
        "permission": "me",
        "money": "me",
    }
    assert _table(db, w["p4"]) == _with(human="agent")


async def test_migrate_is_idempotent_and_keeps_a_set_table(client, ui, db, clock, world):
    w = world
    _set_policy(db, w["p3"], card_delegate={"sub_id": 7})
    _set_policy(db, w["p4"], card_route={"draft": "auto"}, card_delegate={"sub_id": 9})
    first = _migrate(db, clock)
    assert w["p3"] in first and w["p4"] not in first
    assert _table(db, w["p4"]) == _with(draft="auto")  # заданная таблица не тронута
    assert _migrate(db, clock) == []
    assert len(_audits(db, "card_route_migrate")) == len(first)


# --- маршрут карточки (A.1–A.3, П10) ------------------------------------------------------------------------


async def test_route_of_manual_session_project_zero_and_kinds_without_yes(client, ui, db, clock, world):
    w = world
    await _route(ui, w, {"states": "auto", "requests": "auto", "permission": "auto", "turn": "agent"})
    auto_sid = (await _mk(ui, w, mode="auto"))["id"]
    manual_sid = (await _mk(ui, w, mode="manual", role="другая"))["id"]
    with db.reader() as conn:
        p3 = w["p3"]
        assert card_route.route_of(conn, p3, "heavy") == "auto"
        for kind in ("stuck", "stall", "interrupted"):
            assert card_route.route_of(conn, p3, kind) == "agent", kind
        assert card_route.route_of(conn, p3, "turn") == "agent"
        assert card_route.route_of(conn, p3, "errand") == "me"  # вид вне групп
        assert card_route.route_of(conn, 0, "permission") == "me"  # агенты вне проектов
        assert card_route.route_of(conn, None, "permission") == "me"
        assert card_route.for_card(conn, p3, "permission", auto_sid) == "auto"
        assert card_route.for_card(conn, p3, "permission", manual_sid) == "me"
        assert card_route.for_card(conn, p3, "turn", manual_sid) == "me"
        assert card_route.for_card(conn, p3, "permission", None) == "auto"  # внешняя сессия — по таблице


# --- запись и видимость (A.6, П2) ---------------------------------------------------------------------------


async def test_patch_validation_merge_audit_and_bearer(client, ui, db, clock, world):
    w = world
    for bad in (
        {"question": "auto"},  # у вопросов «сразу да» нет
        {"human": "auto"},
        {"agent_msg": "auto"},
        {"nope": "me"},
        {"draft": "maybe"},
        {},
    ):
        await _route(ui, w, bad, expect=422)
    assert _table(db, w["p3"]) == ALL_ME
    await _route(ui, w, {"draft": "auto"})
    await _route(ui, w, {"question": "agent"})  # слияние: draft остался
    assert _table(db, w["p3"]) == _with(draft="auto", question="agent")
    audit = _audits(db, "card_route")[-1]
    assert audit == {
        "project_id": w["p3"],
        "old": _with(draft="auto"),
        "new": _with(draft="auto", question="agent"),
    }
    # Bearer сессии — 403; таблицу bearer не видит.
    r = await client.patch(
        f"{API}/projects/{w['p3']}", json={"card_route": {"draft": "me"}}, headers=_h(w["tok"])
    )
    assert r.status_code == 403, r.text
    assert _table(db, w["p3"])["draft"] == "auto"
    r = await client.get(f"{API}/projects", headers=_h(w["tok"]))
    assert r.status_code == 200 and "card_route" not in r.text
    r = await ui.get(f"{API}/projects")
    shown = r.json()
    rows = shown["projects"] if isinstance(shown, dict) else shown
    mine = next(p for p in rows if p["id"] == w["p3"])
    assert mine["card_route"] == _with(draft="auto", question="agent")


async def test_patch_from_phone_is_allowed(db, clock, monkeypatch, world):
    """A.6: таблицу маршрута владелец меняет и с телефона (удалённый слушатель)."""
    w = world
    monkeypatch.setattr(remote_mod, "SCRYPT_LOG_N", 10)
    notifier = Notifier()
    db.on_commit(notifier.notify)
    state = RemoteState(port=REMOTE_PORT)
    with db.reader() as conn:
        state.load(conn)
    rapp = create_public_app(
        db, clock=clock, port=PORT, remote_port=REMOTE_PORT, remote=state, notifier=notifier
    )
    async with AsyncClient(transport=ASGITransport(app=rapp), base_url=f"http://127.0.0.1:{PORT}") as local:
        cookie, _csrf = await login_via_code(local, db, clock)
        headers = {"Cookie": f"hub_session={cookie}", **csrf_headers(cookie)}
        local.cookies.clear()
        r = await local.put(f"{API}/remote/password", json={"password": PASSWORD}, headers=headers)
        assert r.status_code == 200, r.text
        r = await local.put(f"{API}/remote", json={"enabled": True, "hosts": [REMOTE_HOST]}, headers=headers)
        assert r.status_code == 200, r.text
        async with AsyncClient(
            transport=ASGITransport(app=rapp),
            base_url=f"http://127.0.0.1:{REMOTE_PORT}",
            headers={"Host": REMOTE_HOST},
        ) as phone:
            r = await phone.post(
                f"{API}/auth/password", json={"password": PASSWORD}, headers={"Origin": REMOTE_ORIGIN}
            )
            assert r.status_code == 200, r.text
            phone_headers = {
                "Cookie": f"hub_session={phone.cookies.get('hub_session')}",
                "Origin": REMOTE_ORIGIN,
                "X-Hub-CSRF": r.json()["csrf"],
            }
            phone.cookies.clear()
            r = await phone.patch(
                f"{API}/projects/{w['p3']}", json={"card_route": {"press": "auto"}}, headers=phone_headers
            )
            assert r.status_code == 200, r.text
    assert _table(db, w["p3"]) == _with(press="auto")


# --- «сразу да»: разрешение (C.1, П3) -----------------------------------------------------------------------


async def test_permission_auto_yes_for_auto_session_only(client, ui, db, clock, world):
    w = world
    await _route(ui, w, {"permission": "auto"})
    sid = await _auto(client, ui, w)
    manual = await _auto(client, ui, w, mode="manual", role="другая")
    made = await _ask(client, {"host": w["host"]}, sid)
    assert made == {"id": made["id"], "session_id": sid, "status": "allowed", "decided": "allow"}
    kept = await _ask(client, {"host": w["host"]}, manual)
    assert kept["status"] == "pending" and "decided" not in kept
    with db.reader() as conn:
        request, _rec = held_work._request(conn, made["id"])  # noqa: SLF001
        assert held_work.auto_decided(conn, made["id"]) is True
        assert held_work.auto_decided(conn, kept["id"]) is False
    assert request["status"] == "allowed" and request["delivered_ts"] is not None
    assert json.loads(request["answer_json"]) == {"decision": "allow"}
    events = (await ui.get(f"{API}/sessions/{sid}/events")).json()
    kinds = [(e["kind"], e["summary"]) for e in events if e["kind"] in ("request", "decision")]
    assert kinds[-1] == ("decision", "владелец разрешил: Bash") and kinds[0][0] == "request"
    ref = f"req:{made['id']}"
    queue = _refs(await _queue(client, ui=ui))
    assert ref not in queue and f"req:{kept['id']}" in queue
    assert _system_items(db) == [(ref, "permission", "system", {"by": "auto", "action": "allow"})]
    [audit] = _auto_audits(db)
    assert audit["result"] == "permission" and audit["by"] == "route" and audit["ref"] == ref
    # Таблица `me` — карточка как раньше.
    await _route(ui, w, {"permission": "me"})
    again = await _ask(client, {"host": w["host"]}, sid)
    assert again["status"] == "pending"


async def test_f78_old_route_with_task_kinds_reads_without_error(client, ui, db, clock, world):
    """Ф78-1: задачи по «Го» убраны. Сохранённая таблица с прежними ключами `proposal`/`work_request` (и
    старая карточка `proposal:N`) читается без ошибки: лишние ключи не замечаются, `requests` — без них."""
    w = world
    assert card_route.GROUPS["requests"] == ("agent_request", "rule")
    assert "proposal" not in card_route.YES and "work_request" not in card_route.YES
    _set_policy(db, w["p3"], card_route={"requests": "auto", "proposal": "auto", "work_request": "agent"})
    assert _table(db, w["p3"]) == _with(requests="auto")
    with db.reader() as conn:
        assert card_route.route_of(conn, w["p3"], "rule") == "auto"
        assert card_route.route_of(conn, w["p3"], "proposal") == "me"  # вид вне групп
    r = await ui.get(f"{API}/projects")
    shown = r.json()
    rows = shown["projects"] if isinstance(shown, dict) else shown
    assert next(p for p in rows if p["id"] == w["p3"])["card_route"] == _with(requests="auto")
    await _route(ui, w, {"draft": "auto"})  # запись поверх старой — без отказа
    assert _table(db, w["p3"]) == _with(requests="auto", draft="auto")
    now = _now(clock)
    assert db.write_sync(lambda conn: card_route.auto_answer(conn, "proposal:5", now_ms=now)).done is False


# --- «сразу да»: черновик (C.2–C.4, П4, П11, П12) -----------------------------------------------------------


async def test_draft_auto_yes_same_answer_no_read_mark_and_outcome(client, ui, db, clock, world):
    w = world
    await _sub(client, w, A, role="внешний")
    ident = {"session": base._sess(A)}
    before = await _send(client, w, w["ch"], ident, text="как раньше")
    await _route(ui, w, {"draft": "auto"})
    read_before = _channel(db, w["ch"])[2:]
    made = await _send(client, w, w["ch"], ident, text="сразу да")
    # Сессия видит тот же ответ, что до клика владельца (C.4).
    assert {k: v for k, v in made.items() if k not in ("outbox_id", "message_id")} == {
        k: v for k, v in before.items() if k not in ("outbox_id", "message_id")
    }
    row = _outbox(db, made["outbox_id"])
    assert row["status"] == "queued" and _outbox(db, before["outbox_id"])["status"] == "pending_approval"
    assert _outcomes(db) == [(made["outbox_id"], "approved")]
    assert _channel(db, w["ch"])[2:] == read_before  # П12: без отметки прочтения
    with db.reader() as conn:
        detail = json.loads(
            conn.execute(
                "SELECT detail_json FROM main.audit WHERE action = 'outbox_approve' AND outbox_id = ? "
                "AND actor = 'core'",
                (made["outbox_id"],),
            ).fetchone()[0]
        )
    assert detail["source"] == "auto"
    ref = f"draft:{made['outbox_id']}"
    assert _system_items(db) == [(ref, "draft", "system", {"by": "auto", "action": "approve"})]
    assert ref not in _refs(await _queue(client, ui=ui))


async def test_draft_limit_and_bot_guard_keep_the_card(client, ui, db, clock, world, monkeypatch):
    w = world
    await _sub(client, w, A, role="внешний")
    ident = {"session": base._sess(A)}
    await _route(ui, w, {"draft": "auto"})
    monkeypatch.setattr(autosend, "AUTOSEND_PER_HOUR", 1)
    first = (await _send(client, w, w["ch"], ident, text="раз"))["outbox_id"]
    second = (await _send(client, w, w["ch"], ident, text="два"))["outbox_id"]
    assert _outbox(db, first)["status"] == "queued"
    assert _outbox(db, second)["status"] == "pending_approval"
    held_rows = _auto_audits(db, "held")
    assert [(a["ref"], a["why"]) for a in held_rows] == [(f"draft:{second}", "limit_channel")]
    assert f"draft:{second}" in _refs(await _queue(client, ui=ui))
    # Другая беседа — свой предел; страховка беседы с ботом держит карточку.
    monkeypatch.setattr(bot_guard, "outbox_category", lambda conn, oid, now_ms=None: "payment")
    third = (await _send(client, w, w["ch2"], ident, text="оплати"))["outbox_id"]
    assert _outbox(db, third)["status"] == "pending_approval"
    assert _auto_audits(db, "held")[-1]["why"] == "guard:payment"
    assert f"draft:{third}" in _refs(await _queue(client, ui=ui))


# --- «сразу да»: нажатие кнопки бота, вступление, выход, правило, заявка агента -----------------------------


async def test_press_auto_yes_and_guard(client, ui, db, clock, world, monkeypatch):
    w = world
    await _sub(client, w, A, role="внешний")
    mid = _bot_msg(db, w, clock)
    before = await _press(client, w, mid, _ident(A), 1)
    await _route(ui, w, {"press": "auto"})
    made = await _press(client, w, mid, _ident(A), 2)
    # Ответ сессии — как до клика: карточка «ждёт».
    assert made["press"]["status"] == before["press"]["status"] == "pending"
    with db.reader() as conn:
        assert presses.get(conn, made["press"]["id"]).status == "approved"
        assert presses.get(conn, before["press"]["id"]).status == "pending"
    assert [i[0] for i in _system_items(db)] == [f"press:{made['press']['id']}"]
    monkeypatch.setattr(presses, "_press_guard", lambda conn, ch, mid, text: "payment")
    risky = await _press(client, w, mid, _ident(A), 3)
    with db.reader() as conn:
        assert presses.get(conn, risky["press"]["id"]).status == "pending"
    assert _auto_audits(db, "held")[-1]["why"] == "guard:payment"


async def test_join_auto_yes_after_peek_with_daily_limit(client, ui, db, clock, world, monkeypatch):
    w = world
    sid = await _auto(client, ui, w)
    await _route(ui, w, {"join": "auto"})
    monkeypatch.setattr(join_mod, "AUTO_DAILY", 1)

    async def _join(target):
        body = {"project_id": w["p3"], "target": target, **_me(sid, None)}
        r = await client.post(f"{API}/joins", json=body, headers=_h(w["tok"]))
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def _peek(join_id):
        clock.advance_ms(10 * base.MIN)
        now = _now(clock)

        def _fn(conn):
            leased = join_mod.lease(conn, account_id=w["acc"], now_ms=now, limit=5)
            (item,) = [x for x in leased if x["id"] == join_id]
            peek = {"title": "Канал", "kind": "broadcast", "member": False, "ext_id": f"-100{join_id}"}
            return join_mod.report(
                conn, account_id=w["acc"], request_id=join_id, lease_gen=item["lease_gen"], outcome="done",
                now_ms=now, peek=peek,
            )  # fmt: skip

        return db.write_sync(_fn)

    first = await _join("@first_one")
    second = await _join("@second_one")
    assert _peek(first) == "approved"
    assert _peek(second) == "pending"  # предел за сутки — карточка владельцу
    assert [a["why"] for a in _auto_audits(db, "held")] == ["limit_daily"]
    with db.reader() as conn:
        assert join_mod.get(conn, first).auto == 0  # «да» таблицы — не автоодобрение проекта
    assert [i[0] for i in _system_items(db)] == [f"join:{first}"]


async def test_leave_rule_and_agent_request_auto_yes(client, ui, db, clock, world):
    w = world
    sid = await _auto(client, ui, w)
    me = _me(sid, None)
    await _route(ui, w, {"join": "auto", "requests": "auto"})
    r = await client.post(f"{API}/leaves", json={"channel_id": w["ch2"], **me}, headers=_h(w["tok"]))
    assert r.status_code == 201, r.text
    leave_ref = f"leave:{r.json()['id']}"
    r = await client.post(
        f"{API}/rule-requests",
        json={"project_id": w["p3"], "account_id": w["acc"], "kind": "all", **me},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 201, r.text
    rule_ref = f"rule:{r.json()['id']}"
    body = {"kind": "new_session", "project_id": w["p3"], "role": "Тестер", "task": "прогнать тесты", **me}
    r = await client.post(f"{API}/agent-requests", json=body, headers=_h(w["tok"]))
    assert r.status_code == 201, r.text
    agent_ref = f"agent_request:{r.json()['id']}"
    refs = [leave_ref, rule_ref, agent_ref]
    assert [i[0] for i in _system_items(db)] == refs
    assert not set(refs) & set(_refs(await _queue(client, ui=ui)))
    with db.reader() as conn:
        statuses = [
            conn.execute(
                f"SELECT status FROM main.{table} WHERE id = ?", (int(ref.split(":")[1]),)
            ).fetchone()[0]  # noqa: S608
            for table, ref in (
                ("leave_request", leave_ref),
                ("rule_request", rule_ref),
                ("agent_request", agent_ref),
            )
        ]
        roles = [r[0] for r in conn.execute("SELECT role FROM main.hub_session ORDER BY id")]
    assert statuses[0] != "pending" and statuses[1] != "pending" and statuses[2] != "pending"
    assert "Тестер" in roles  # заявка на напарника заведена ответом ядра


# --- «сразу да»: напоминание и состояния (П5, П9, П10) ------------------------------------------------------


async def test_reminder_auto_waits_for_the_signal(client, ui, db, clock, world):
    w = world
    sid = await _auto(client, ui, w)
    await _route(ui, w, {"reminder": "auto"})
    with db.reader() as conn:
        sub_id = conn.execute(
            "SELECT id FROM main.session_sub WHERE session_pid = ? AND ended_ts IS NULL",
            (held.held_pid(sid),),
        ).fetchone()[0]
    r = await client.post(
        f"{API}/reminders",
        json={"channel_id": w["ch"], "at": _iso(clock, 60), "note": "проверить", **_me(sid, None)},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 201, r.text
    rem_id = r.json()["id"] if "id" in r.json() else r.json()["reminder"]["id"]
    now = _now(clock)
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.reminder SET status = 'fired', fired_ts = ?, signal_sub_id = ? WHERE id = ?",
            (now, sub_id, rem_id),
        )
    )
    ref = f"reminder:{rem_id}"
    assert ref not in _refs(await _queue(client, ui=ui))  # владельцу карточки нет
    assert _sweep(db, clock) == []  # сигнал сессии ещё не дошёл
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.reminder SET signaled_ts = ? WHERE id = ?", (now, rem_id))
    )
    assert _sweep(db, clock) == [ref]
    with db.reader() as conn:
        assert (
            conn.execute("SELECT status FROM main.reminder WHERE id = ?", (rem_id,)).fetchone()[0] == "done"
        )
    assert _system_items(db) == [(ref, "reminder", "system", {"by": "auto", "action": "done"})]
    # Маршрут `me` — карточка владельцу, как прежде.
    await _route(ui, w, {"reminder": "me"})
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.reminder SET status = 'fired' WHERE id = ?", (rem_id,))
    )
    assert ref in _refs(await _queue(client, ui=ui))


async def test_heavy_state_auto_yes_once_per_state_and_interrupted_to_agent(
    client, ui, db, clock, world, monkeypatch
):
    """Ревью Р6: пауза лимита (`paused`) из «сразу да» убрана (test_f66_review.py) — «один раз на состояние»
    проверяется на тяжёлой сессии (`heavy`)."""
    w = world
    sid = await _auto(client, ui, w)
    await _route(ui, w, {"states": "auto"})
    r = await ui.post(f"{API}/sessions/{sid}/say", {"text": "сделай отчёт"})
    assert r.status_code == 200, r.text
    say_id = r.json()["id"]
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.hub_session SET wait_reason = 'heavy' WHERE id = ?", (sid,))
    )
    monkeypatch.setattr(held_signals, "pending_total", lambda *a, **k: 3)
    ref = f"heavy:{sid}"
    assert _sweep(db, clock) == [ref]
    with db.reader() as conn:
        assert held.get(conn, sid).wake_requested_ts == _now(clock)
    # Состояние не ушло, ход тот же — второй раз «да» нет: карточка остаётся владельцу.
    clock.advance_ms(base.MIN)
    assert _sweep(db, clock) == []
    assert ref in _refs(await _queue(client, ui=ui))
    assert _system_items(db) == [(ref, "heavy", "system", {"by": "auto", "action": "wake", "turns": 0})]
    # Новый ход — новое состояние: «да» снова.
    db.write_sync(lambda conn: conn.execute("UPDATE main.hub_session SET turns = 1 WHERE id = ?", (sid,)))
    assert _sweep(db, clock) == [ref]
    # П10: прерванная реплика при `auto` — агенту, ядро на неё не отвечает.
    db.write_sync(lambda conn: held_work.mark_interrupted(conn, held.get(conn, sid), say_id, _now(clock)))
    queue = _refs(await _queue(client, ui=ui))
    assert queue[f"say:{say_id}"]["kind"] == "interrupted" and queue[f"say:{say_id}"]["in_channel"] is True
    _sweep(db, clock)
    assert f"say:{say_id}" not in [i[0] for i in _system_items(db)]


async def test_route_me_or_agent_never_answers(client, ui, db, clock, world):
    """Без `auto` ядро не отвечает ни на что: `auto_answer` — `Auto(False)`, записей нет."""
    w = world
    sid = await _auto(client, ui, w)
    await _route(ui, w, {"permission": "agent"})
    made = await _ask(client, {"host": w["host"]}, sid)
    assert made["status"] == "pending"
    now = _now(clock)
    assert (
        db.write_sync(lambda conn: card_route.auto_answer(conn, f"req:{made['id']}", now_ms=now)).done
        is False
    )
    assert _system_items(db) == [] and _auto_audits(db) == []
    assert _refs(await _queue(client, ui=ui))[f"req:{made['id']}"]["in_channel"] is True


# --- 23.10: «Ответ сессии» — «сразу да» -----------------------------------------------------------------


async def test_turn_auto_no_card_without_owner_line_ask_goes_to_agent(client, ui, db, world):
    """Владелец 23.10: «мусорные карточки, которые от агента требуют только принять». `turn = auto`: ответ на
    реплику владельца без «ВЛАДЕЛЬЦУ:» карточки не даёт; со «ВЛАДЕЛЬЦУ:» — карточка, но владельцу (24.10: ответ на его
    же реплику
    читает он сам, не агент); ручная сессия — как прежде владельцу."""
    from .test_owner_turn import ASK, TAIL, _say, _turn_cards, _Turns

    w = world
    await _route(ui, w, {"turn": "auto"})
    sid = await _auto(client, ui, w)
    turns = _Turns(client, w, sid)
    say_id = await _say(ui, sid)
    await turns.start("say")
    await turns.deliver(say_id)
    await turns.end(TAIL)
    assert _turn_cards(await _queue(client, ui=ui)) == {}
    say_id = await _say(ui, sid, "а теперь?")
    await turns.start("say")
    await turns.deliver(say_id)
    await turns.end(ASK)
    ref = f"turn:{sid}:2"
    assert list(_turn_cards(await _queue(client, ui=ui))) == [ref]
    with db.reader() as conn:
        assert card_route.for_card(conn, w["p3"], "turn", sid, ref=ref) == "me"

    manual = await _auto(client, ui, w, mode="manual")
    turns = _Turns(client, w, manual)
    say_id = await _say(ui, manual)
    await turns.start("say")
    await turns.deliver(say_id)
    await turns.end(TAIL)
    assert f"turn:{manual}:1" in _turn_cards(await _queue(client, ui=ui))


async def test_turn_auto_ask_without_owner_line_still_goes_to_agent(client, ui, db, world):
    """Вопрос владельца: «а если модель ждёт ответа, а «ВЛАДЕЛЬЦУ:» не написала?» — у «сразу да» ответ без «ВЛАДЕЛЬЦУ:»
    с вопросом или просьбой в конце карточку даёт (агенту); «ничего не нужно» и вопрос в блоке кода — нет."""
    from .test_owner_turn import _answer_body, _say, _turn_cards, _Turns

    w = world
    await _route(ui, w, {"turn": "auto"})
    sid = await _auto(client, ui, w)
    turns = _Turns(client, w, sid)
    cases = [
        ("Обработчик переехал.\n\nСтарый сервис держим неделю как откат — ок?", True),
        ("Манифесты готовы.\n\nСейчас нужно твоё слово, выкладывать ли их.", True),
        ("Сделал.\n\nЕсли хочешь, скажи «бери 7439».\n\nСейчас ничего не нужно.", False),
        ("Отклик ушёл:\n\n```\nДобрый день! Получилось посмотреть?\n```\n\nЖду ответа клиента.", False),
    ]
    for n, (body, card) in enumerate(cases, 1):
        say_id = await _say(ui, sid, f"реплика {n}")
        await turns.start("say")
        await turns.deliver(say_id)
        last = body.rsplit("\n\n", 1)[-1]
        await turns.end(_answer_body(body, last, "tail"))
        assert (f"turn:{sid}:{n}" in _turn_cards(await _queue(client, ui=ui))) is card, body
