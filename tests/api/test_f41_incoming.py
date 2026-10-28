"""Ф41-4а (docs/architecture.md и 6, поправки раздела 10): входящее постоянной
сессии хаба на уровне ядра — желаемый фильтр подписки, раздел `sessions.signals` ожидания хоста, выдача
сигналов хосту, `listening`, «Поднять», ход по сигналу, оборванный выключением.

Хост поддельный: bearer `work_host` зовёт ручки ядра напрямую. Помощники и «мир» — из test_hub_sessions.py."""

import asyncio
import sqlite3

import pytest

from hub.core import held, held_signals, security, subs

from . import test_hub_sessions as base
from .conftest import insert_channel, insert_message
from .test_hub_sessions import (
    CODER,
    MIN,
    ROLE,
    B,
    _at,
    _audits,
    _ev,
    _h,
    _hs,
    _hsub,
    _ingest,
    _mk,
    _now,
    _sess,
    _sub,
    _token,
    _wait,
)

# Фикстуры «мира» сессий хаба — те же, что у тестов 41-2а.
procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"
PROC = {"proc_pid": 4242, "proc_start": 777_000}
WANT_KEYS = {
    "mode": "messages",
    "msg_filter": "personal",
    "keywords": ["Договор", "цена"],
    "exclude": ["спам"],
    "batch_min": 0,
    "accounts": None,
}


async def _host(client, w, path, body=None, *, expect=200, params=None):
    headers = _h(w["host"])
    if body is None:
        r = await client.get(f"{API}/work/host/{path}", headers=headers, params=params)
    else:
        r = await client.post(f"{API}/work/host/{path}", json=body, headers=headers)
    assert r.status_code == expect, r.text
    return r.json()


async def _idle(client, w, hub_id):
    """Поддельный хост: процесс сессии жив и ждёт входа."""
    return await _host(client, w, f"sessions/{hub_id}/state", {"state": "idle", **PROC})


async def _signals(client, w, hub_id, *, expect=200):
    return await _host(client, w, f"sessions/{hub_id}/signals", {}, expect=expect)


async def _wait_host(client, w, *, known=None, timeout_s=0, since=None):
    if since is None:
        since = (await _host(client, w, "open"))["max_seq"]
    params = {"since": since, "timeout_s": timeout_s}
    if known is not None:
        params["signals_known"] = ",".join(f"{sid}:{n}" for sid, n in known.items())
    return await _host(client, w, "wait", params=params)


async def _session(ui, hub_id) -> dict:
    r = await ui.get(f"{API}/sessions/{hub_id}")
    assert r.status_code == 200, r.text
    return r.json()["session"]


async def _peer(client, w, role=ROLE, text="передай клиенту") -> int:
    """Сообщение агента линии роли — от внешней сессии другого проекта."""
    r = await client.post(
        f"{API}/peer-msgs",
        json={"text": text, "to": {"project_id": w["p3"], "role": role}, "session": _sess(B)},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 201, r.text
    return r.json()["msg"]["id"]


def _triage(db) -> str:
    return _token(db, name="agent:triage", kind="background", scopes=("read", "triage"))[0]


async def _card(client, triage, channel_id, message_ids, label="reply") -> int:
    r = await client.post(
        f"{API}/agent/items",
        json={
            "channel_id": channel_id,
            "message_ids": message_ids,
            "label": label,
            "summary": "сводка",
            "action": None,
            "provider": "test",
            "model": "m",
        },
        headers=_h(triage),
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


# --- 2.1, П9, П16: желаемый фильтр в записи -----------------------------------------------------------------


async def test_want_sub_create_normalized_and_forms(client, ui, db, world):
    w = world
    made = await _mk(ui, w, role=ROLE, want_sub=WANT_KEYS)
    # Нормализовано кодом подписки: регистр ключей, порядок — как у `POST /subs`.
    assert made["want_sub"] == {**WANT_KEYS, "keywords": ["договор", "цена"]}
    assert made["sub"] is None and made["sub_id"] is None
    assert made["sub_stale"] is True and made["autosend_channels"] == 0
    assert made["listening"] is False

    plain = await _mk(ui, w)
    assert plain["want_sub"] is None and plain["sub_stale"] is False and plain["sub"] is None

    # Неполная форма: умолчания подставляет ядро; `direct` — без фильтра и ключей.
    direct = await _mk(ui, w, role=CODER, want_sub={"mode": "direct"})
    assert direct["want_sub"] == {
        "mode": "direct",
        "msg_filter": "all",
        "keywords": [],
        "exclude": [],
        "batch_min": None,
        "accounts": None,
    }
    # Форма окна шлёт пустые списки всегда — это «ключей нет», а не отказ.
    cards = await _mk(
        ui,
        w,
        want_sub={"mode": "cards", "keywords": [], "exclude": [], "batch_min": None, "accounts": None},
    )
    assert cards["want_sub"]["mode"] == "cards" and cards["want_sub"]["keywords"] == []

    # Форма для хоста и элемент `/sync`.
    rows = {row["id"]: row for row in (await _host(client, w, "sessions"))["items"]}
    host_row = rows[made["id"]]
    assert host_row["want_sub"] == made["want_sub"] and host_row["sub_stale"] is True
    assert host_row["sub_id"] is None and host_row["signal_lost"] is False
    assert host_row["wake_requested_ts"] is None and host_row["compact_requested_ts"] is None
    assert host_row["cache_expires_ts"] is None and host_row["context_tokens"] is None
    r = await ui.get(f"{API}/sync", params={"since": 0})
    synced = {row["id"]: row for row in r.json()["hub_sessions"]}
    assert synced[made["id"]]["want_sub"] == made["want_sub"]
    assert synced[made["id"]]["sub_stale"] is True and "listening" not in synced[made["id"]]
    assert _audits(db, "hub_session_create")[0]["want_sub"]["keys"] == 2  # в аудите — числа, не тексты


@pytest.mark.parametrize(
    ("want", "text"),
    [
        ({"mode": "all"}, "mode: cards, messages или direct"),
        ({"mode": "cards", "msg_filter": "personal"}, "personal — только с mode=messages"),
        ({"mode": "messages", "msg_filter": "all", "keywords": ["цена"]}, "ключи — только"),
        ({"mode": "messages", "msg_filter": "personal", "exclude": ["спам"]}, "exclude — только вместе"),
        ({"mode": "direct", "batch_min": 5}, "mode=direct — без"),  # Ф67-1: без ключей пачка можно
        (
            {"mode": "messages", "msg_filter": "personal", "keywords": ["цена"], "batch_min": 9999},
            "batch_min: 0–",
        ),
        ({"mode": "direct", "keywords": ["цена"]}, "mode=direct — без"),
        ({"mode": "cards", "accounts": {"only": [999]}}, "нет аккаунтов 999"),
        ({"mode": "cards", "accounts": {"both": [1]}}, "accounts:"),
        ({"mode": "cards", "role": "x"}, "лишние поля role"),
        ({"mode": "messages", "keywords": "цена"}, "keywords: список строк"),
    ],
)
async def test_want_sub_refusals_carry_text(ui, db, world, want, text):
    r = await ui.post(f"{API}/sessions", {"project_id": world["p3"], "engine": "claude", "want_sub": want})
    assert r.status_code == 422, r.text
    assert isinstance(r.json()["detail"], str) and text in r.json()["detail"]
    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM main.hub_session").fetchone()[0] == 0


async def test_patch_want_sub(client, ui, db, world):
    w = world
    made = await _mk(ui, w, role=ROLE)
    hub_id = made["id"]
    path = f"{API}/sessions/{hub_id}"
    # Только владелец из интерфейса.
    for token in (w["tok"], w["host"]):
        r = await client.patch(path, json={"want_sub": {"mode": "direct"}}, headers=_h(token))
        assert r.status_code == 403, r.text
    r = await ui.c.patch(path, json={"want_sub": {"mode": "direct"}}, headers=ui.headers)
    assert r.status_code == 200, r.text
    row = r.json()["session"]
    assert row["want_sub"]["mode"] == "direct" and row["sub_stale"] is True
    assert row["seq"] > made["seq"]  # хост и интерфейс видят правку
    # Другие поля ручка не правит, лишнее поле — отказ модели.
    r = await ui.c.patch(path, json={"want_sub": None, "title": "x"}, headers=ui.headers)
    assert r.status_code == 422
    r = await ui.c.patch(path, json={"want_sub": {"mode": "nope"}}, headers=ui.headers)
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)
    # Подписка по желаемому — расхождения нет; правка желаемого его возвращает, живую подписку не трогает.
    sub_id = (await _hsub(client, w, hub_id, mode="direct"))["sub"]["id"]
    row = await _session(ui, hub_id)
    assert row["sub_stale"] is False and row["sub"]["id"] == sub_id and row["sub"]["mode"] == "direct"
    r = await ui.c.patch(path, json={"want_sub": {"mode": "cards"}}, headers=ui.headers)
    assert r.json()["session"]["sub_stale"] is True and r.json()["session"]["sub"]["id"] == sub_id
    r = await ui.c.patch(path, json={"want_sub": None}, headers=ui.headers)
    assert r.json()["session"]["want_sub"] is None and r.json()["session"]["sub_stale"] is False
    assert r.json()["session"]["sub"]["id"] == sub_id  # «не подписывать» чужую подписку не снимает
    assert [a["want_sub"] and a["want_sub"]["mode"] for a in _audits(db, "hub_session_want_sub")] == [
        "direct",
        "cards",
        None,
    ]
    # Закрытая — 409; нет записи — 404.
    assert (await ui.post(f"{path}/close")).status_code == 200
    r = await ui.c.patch(path, json={"want_sub": None}, headers=ui.headers)
    assert r.status_code == 409 and r.json()["detail"] == "сессия хаба закрыта"
    r = await ui.c.patch(f"{API}/sessions/999", json={"want_sub": None}, headers=ui.headers)
    assert r.status_code == 404


async def test_sub_in_record_stale_and_seq_on_subscribe(client, ui, db, world):
    """`sub` и `sub_stale` в записи; подписка и её конец двигают `seq` сессии хаба (окно и хост видят)."""
    w = world
    made = await _mk(ui, w, role=ROLE, want_sub=WANT_KEYS)
    hub_id = made["id"]
    seq0 = (await _session(ui, hub_id))["seq"]
    body = await _hsub(
        client,
        w,
        hub_id,
        mode="messages",
        msg_filter="personal",
        keywords=["договор", "цена"],
        exclude=["спам"],
        batch_min=0,
        accounts=[],
    )
    row = await _session(ui, hub_id)
    assert row["seq"] > seq0
    assert row["sub"] == {
        "id": body["sub"]["id"],
        "mode": "messages",
        "msg_filter": "personal",
        "keywords": ["договор", "цена"],
        "exclude": ["спам"],
        "batch_min": 0,
        "accounts": None,
        "pending": 0,
    }
    assert row["sub_stale"] is False and row["sub_id"] == body["sub"]["id"]
    # Другие ключи — расхождение.
    await _hsub(client, w, hub_id, mode="messages", msg_filter="personal", keywords=["договор"])
    assert (await _session(ui, hub_id))["sub_stale"] is True
    # Конец подписки (владелец снял) — запись снова «нет подписки», `seq` вырос.
    seq1 = (await _session(ui, hub_id))["seq"]
    r = await ui.post(f"{API}/subs/{body['sub']['id']}/end")
    assert r.status_code == 200, r.text
    row = await _session(ui, hub_id)
    assert row["sub"] is None and row["sub_stale"] is True and row["seq"] > seq1
    # Подписка ВНЕШНЕЙ сессии записей сессий хаба не трогает.
    seq2 = row["seq"]
    await _sub(client, w, B, cwd=w["dir4"], role=CODER)
    assert (await _session(ui, hub_id))["seq"] == seq2


async def test_autosend_count(client, ui, db, world):
    w = world
    made = await _mk(ui, w, role=ROLE)
    other = await _mk(ui, w, role=CODER)
    r = await ui.c.put(
        f"{API}/channels/{w['ch']}/autosend", json={"role": ROLE, "on": True}, headers=ui.headers
    )
    assert r.status_code == 200, r.text
    assert (await _session(ui, made["id"]))["autosend_channels"] == 1
    assert (await _session(ui, other["id"]))["autosend_channels"] == 0
    count = f"{API}/sessions/autosend-count"
    r = await ui.get(count, params={"project_id": w["p3"], "role": "Маркетолог"})
    assert r.status_code == 200 and r.json() == {"count": 1}
    assert (await ui.get(count, params={"project_id": w["p4"], "role": ROLE})).json() == {"count": 0}
    assert (await ui.get(count, params={"project_id": w["p3"], "role": ""})).json() == {"count": 0}
    assert (await ui.get(count, params={"project_id": w["p3"]})).json() == {"count": 0}
    # Только владелец: bearer — 403.
    r = await client.get(count, params={"project_id": w["p3"], "role": ROLE}, headers=_h(w["tok"]))
    assert r.status_code == 403
    # Беседа в архиве не считается.
    db.write_sync(lambda conn: conn.execute("UPDATE main.channel SET archived = 1 WHERE id = ?", (w["ch"],)))
    assert (await ui.get(count, params={"project_id": w["p3"], "role": ROLE})).json() == {"count": 0}


# --- 2.3, П2: что готово; пробуждение ожидания хоста --------------------------------------------------------


async def test_host_wait_signals_ready_known_and_debounce(client, ui, db, clock, world):
    w = world
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    sub_id = (await _hsub(client, w, hub_id, mode="direct"))["sub"]["id"]
    await _sub(client, w, B, cwd=w["dir4"], role=CODER)
    opened = await _host(client, w, "open")
    assert opened["sessions"]["signals"] == []
    since = opened["max_seq"]

    # Ожидание без `signals_known` (хост старше 41-4) сигналами не будится.
    waiting = asyncio.create_task(_wait_host(client, w, known={}, timeout_s=5, since=since))
    await asyncio.sleep(0.05)
    assert not waiting.done()
    await _peer(client, w)
    data = await asyncio.wait_for(waiting, 2)
    assert data["sessions"]["signals"] == [{"id": hub_id, "ready": 1, "urgent": 0, "next_ms": None}]
    assert data["sessions"]["items"] == []  # запись сессии не менялась — разбудил раздел готовности
    since = data["max_seq"]
    old = await _host(client, w, "wait", params={"since": since, "timeout_s": 0})
    assert old["sessions"]["signals"] == [{"id": hub_id, "ready": 1, "urgent": 0, "next_ms": None}]

    # Хост знает про готовое и не забирает (сессия занята) — ожидание не крутится вхолостую.
    known = asyncio.create_task(_wait_host(client, w, known={hub_id: 1}, timeout_s=0.3, since=since))
    await asyncio.sleep(0.1)
    assert not known.done()
    await known
    # В записи — `pending`.
    assert (await _session(ui, hub_id))["sub"]["pending"] == 1

    # Выдача: один раз; дальше готового нет, известное хосту «1» — уже отличие (будит сразу).
    await _idle(client, w, hub_id)
    body = await _signals(client, w, hub_id)
    assert body["sub_id"] == sub_id and [s["kind"] for s in body["signals"]] == ["peer"]
    assert (await _signals(client, w, hub_id))["signals"] == []
    data = await _wait_host(client, w, known={hub_id: 1}, timeout_s=5)
    assert data["sessions"]["signals"] == []

    # Дебаунс `direct` — 5 с от выдачи: новое сообщение не готово, `next_ms` — конец окна; ожидание само
    # просыпается к его концу (часы теста стоят — двигаем их).
    await _peer(client, w, text="второе")
    data = await _wait_host(client, w, known={})
    waiting = {"id": hub_id, "ready": 0, "urgent": 0, "next_ms": subs.CHAN_DEBOUNCE_MS}
    assert data["sessions"]["signals"] == [waiting]
    assert (await _signals(client, w, hub_id))["signals"] == []  # под дебаунсом выдачи нет
    clock.advance_ms(subs.CHAN_DEBOUNCE_MS)
    data = await _wait_host(client, w, known={})
    assert data["sessions"]["signals"] == [{"id": hub_id, "ready": 1, "urgent": 0, "next_ms": None}]
    assert [s["kind"] for s in (await _signals(client, w, hub_id))["signals"]] == ["peer"]


async def test_host_wait_next_ms_of_keyword_batch(client, ui, db, clock, world):
    """Пачка «по ключу»: сигнал ждёт срока — `ready` 0 и `next_ms` до конца пачки; пачка выдачу не зовёт."""
    w = world
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    await _hsub(client, w, hub_id, mode="messages", msg_filter="personal", keywords=["договор"], batch_min=5)
    clock.advance_ms(MIN)
    _ingest(db, w, [_ev("team", "201", _now(clock), text="пришлите договор")], clock)
    data = await _wait_host(client, w, known={})
    (item,) = data["sessions"]["signals"]
    assert item["id"] == hub_id and item["ready"] == 0 and 0 < item["next_ms"] <= 6 * MIN
    assert (await _session(ui, hub_id))["sub"]["pending"] == 1
    await _idle(client, w, hub_id)
    assert (await _signals(client, w, hub_id))["signals"] == []  # срок не пришёл — не выдаётся
    clock.advance_ms(item["next_ms"])
    data = await _wait_host(client, w, known={})
    assert data["sessions"]["signals"] == [{"id": hub_id, "ready": 1, "urgent": 0, "next_ms": None}]
    (signal,) = (await _signals(client, w, hub_id))["signals"]
    assert signal["kind"] == "messages" and signal["cat"] == "keyword" and signal["keys"] == ["договор"]


# --- 2.3: выдача хосту = выдача прежних ручек ожидания ---------------------------------------------------


def _shape(signal: dict) -> dict:
    """Сигнал без номеров, которые у двух сессий различаются по построению теста."""
    return {k: v for k, v in signal.items() if k not in ("sub_id", "msg_id", "reminder_id", "outbox_id")}


async def test_delivery_to_host_equals_wait_handlers(client, ui, db, clock, app, world):
    """Две пары сессий хаба с одинаковыми подписками в одном проекте: одна сессия пары забирает сигналы
    прежними ручками ожидания своим токеном, другая — хост. Каждой — своя копия сигналов на тех же данных."""
    w = world
    triage = _triage(db)
    roles = {"card_x": "карты один", "card_y": "карты два", "msg_x": "ключи один", "msg_y": "ключи два"}
    ids = {name: (await _mk(ui, w, role=role))["id"] for name, role in roles.items()}
    subs_ids = {}
    for name in ("card_x", "card_y"):
        subs_ids[name] = (await _hsub(client, w, ids[name], mode="cards"))["sub"]["id"]
    for name in ("msg_x", "msg_y"):
        body = await _hsub(
            client, w, ids[name], mode="messages", msg_filter="personal", keywords=["договор"], batch_min=0
        )
        subs_ids[name] = body["sub"]["id"]
    # Подписка на беседу — у обеих сессий «сообщений».
    chan_ids = {}
    for name in ("msg_x", "msg_y"):
        r = await client.post(
            f"{API}/chan-subs",
            json={"channel_id": w["ch2"], "session": _hs(ids[name], cwd=w["dir3"])},
            headers=_h(w["tok"]),
        )
        assert r.status_code == 200, r.text
        chan_ids[name] = r.json()["sub"]["id"]
    clock.advance_ms(MIN)
    # Данные: карточка, сообщение «по ключу», сообщение в беседе под подпиской, напоминание, сообщение агента
    # и владельца, исход черновика — каждой сессии своё, одинаковое.
    acc = w["acc"]
    cards_ch = insert_channel(db, acc, project_id=w["p3"], ext_id="cards", kind="dm", title="Клиент")
    cards_msg = insert_message(db, cards_ch, ext_id="10", ts=_now(clock) - MIN)
    await _card(client, triage, cards_ch, [cards_msg], label="urgent")
    _ingest(db, w, [_ev("team", "201", _now(clock), text="пришлите договор")], clock)
    _ingest(db, w, [_ev("other", "301", _now(clock), text="привет")], clock)
    await _sub(client, w, B, cwd=w["dir4"], role=CODER)
    for name, role in roles.items():
        me = _hs(ids[name])
        r = await client.post(
            f"{API}/reminders",
            json={"channel_id": w["ch"], "at": _at(clock, 5), "note": "спросить", "session": me},
            headers=_h(w["tok"]),
        )
        assert r.status_code == 201, r.text
        await _peer(client, w, role=role)
        r = await ui.post(
            f"{API}/peer-msgs", {"text": "от владельца", "to": {"project_id": w["p3"], "role": role}}
        )
        assert r.status_code == 201, r.text
        r = await client.post(
            f"{API}/messages/send",
            json={"channel_id": w["ch"], "text": "черновик", "idempotency_key": f"k-{name}", "session": me},
            headers=_h(w["tok"]),
        )
        assert r.status_code == 200 and r.json()["status"] == "pending_approval", r.text
        assert (await ui.post(f"{API}/outbox/{r.json()['outbox_id']}/cancel")).status_code == 200
    clock.advance_ms(6 * MIN)  # напоминания сработали
    from hub.core import reminders as reminders_mod

    db.write_sync(lambda conn: reminders_mod.fire_due(conn, _now(clock)))

    # Сначала хост (сессии Y), потом прежние ручки (сессии X): выдача одной сессии другой не касается.
    got = {}
    for name in ("card_y", "msg_y"):
        await _idle(client, w, ids[name])
        got[name] = await _signals(client, w, ids[name])
        assert got[name]["sub_id"] == subs_ids[name] and got[name]["project_id"] == w["p3"]
    old = {}
    for name in ("card_x", "msg_x"):
        old[name] = await _wait(client, w, subs_ids[name], _hs(ids[name]))
    r = await client.post(
        f"{API}/chan-subs/wait",
        json={"session": _hs(ids["msg_x"]), "waiter": "0123456789abcdef", "timeout_s": 1},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
    old_chan = r.json()["signals"]

    for x, y in (("card_x", "card_y"), ("msg_x", "msg_y")):
        assert [_shape(s) for s in got[y]["signals"]] == [_shape(s) for s in old[x]], (x, y)
    kinds = [s.get("kind", "card") for s in got["card_y"]["signals"]]
    assert kinds == ["card", "reminder", "peer", "peer", "draft"]
    assert [s.get("kind") for s in got["msg_y"]["signals"]] == [
        "messages",
        "reminder",
        "peer",
        "peer",
        "draft",
    ]
    assert got["card_y"]["signals"][0]["label"] == "urgent"
    assert got["card_y"]["chan_signals"] == []
    assert [_shape(s) for s in got["msg_y"]["chan_signals"]] == [_shape(s) for s in old_chan]
    assert [s["sub_id"] for s in got["msg_y"]["chan_signals"]] == [chan_ids["msg_y"]]
    assert got["msg_y"]["chan_signals"][0]["others"] == 1
    # Форма годится прежним проверкам клиента ожидания (хост пропускает ответ через них же).
    from hub import sub_wait

    for name in ("card_y", "msg_y"):
        _sid, _pid, valid = sub_wait.valid_signals(got[name])
        assert len(valid) == len(got[name]["signals"])
    assert len(sub_wait.valid_chan_signals({"signals": got["msg_y"]["chan_signals"]})) == 1

    # Один раз: повторная выдача пуста у обеих сторон; отметки в базе — одинаковые.
    for name in ("card_y", "msg_y"):
        again = await _signals(client, w, ids[name])
        assert again["signals"] == [] and again["chan_signals"] == []
    with db.reader() as conn:
        statuses = dict(conn.execute("SELECT sub_id, status FROM main.session_signal ORDER BY id").fetchall())
        chan_statuses = conn.execute(
            "SELECT COALESCE(project_sub_id, -chan_sub_id), status FROM main.session_chan_signal ORDER BY id"
        ).fetchall()
        last_wait = dict(conn.execute("SELECT id, last_wait_ts FROM main.session_sub").fetchall())
        tokens = {
            row[0]
            for row in conn.execute(
                "SELECT api_token_id FROM main.audit WHERE action = 'sub_signal' AND result = 'delivered'"
            )
        }
    assert statuses == {subs_ids["card_x"]: "delivered", subs_ids["card_y"]: "delivered"}
    assert {status for _sub, status in chan_statuses} == {"delivered"} and len(chan_statuses) == 4
    assert last_wait[subs_ids["card_y"]] == _now(clock)  # П14: `last_wait_ts` — при выдаче хосту
    assert tokens == {w["tok_id"]}  # П14: аудит — с токеном-создателем подписки, не хоста


async def test_delivery_validity_skips_like_wait(client, ui, db, clock, app, world):
    """Годность при выдаче: отвеченная, прочитанная, перенесённая карточка и беседа перед глазами владельца не
    считаются готовыми и не выдаются; годная — выдаётся один раз."""
    w = world
    triage = _triage(db)
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    sub_id = (await _hsub(client, w, hub_id, mode="cards"))["sub"]["id"]
    chans = {}
    for name in ("read", "answered", "present", "moved", "ok"):
        cid = insert_channel(db, w["acc"], project_id=w["p3"], ext_id=name, kind="dm")
        mid = insert_message(db, cid, ext_id="10", ts=10_000)
        chans[name] = (cid, mid)
        await _card(client, triage, cid, [mid])
    # 21.10: карточки по личкам — личное, для сессии хаба срочно.
    assert (await _wait_host(client, w, known={}))["sessions"]["signals"] == [
        {"id": hub_id, "ready": 5, "urgent": 5, "next_ms": None}
    ]
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET last_read_ts = 10000, last_read_sort = 10, last_read_ext = '10' "
            "WHERE id = ?",
            (chans["read"][0],),
        )
    )
    insert_message(db, chans["answered"][0], ext_id="11", ts=11_000, is_me=1)
    app.state.presence.set(
        token_id=1, client_id="tab", channel_id=chans["present"][0], thread_ext_id=None, now_ms=_now(clock)
    )
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET project_id = ? WHERE id = ?", (w["p4"], chans["moved"][0])
        )
    )
    assert (await _wait_host(client, w, known={}))["sessions"]["signals"] == [
        {"id": hub_id, "ready": 1, "urgent": 1, "next_ms": None}
    ]
    assert (await _session(ui, hub_id))["sub"]["pending"] == 1
    await _idle(client, w, hub_id)
    body = await _signals(client, w, hub_id)
    assert [s["channel_id"] for s in body["signals"]] == [chans["ok"][0]]
    with db.reader() as conn:
        rows = dict(
            conn.execute(
                "SELECT channel_id, COALESCE(reason, status) FROM main.session_signal WHERE sub_id = ?",
                (sub_id,),
            ).fetchall()
        )
    assert rows == {
        chans["read"][0]: "read",
        chans["answered"][0]: "answered",
        chans["present"][0]: "present",
        chans["moved"][0]: "moved",
        chans["ok"][0]: "delivered",
    }
    assert (await _wait_host(client, w, known={}))["sessions"]["signals"] == []

    # Все ждущие негодны — готового нет, спящая сессия на устаревшее не поднимается.
    cid = insert_channel(db, w["acc"], project_id=w["p3"], ext_id="late", kind="dm")
    mid = insert_message(db, cid, ext_id="10", ts=10_000)
    await _card(client, triage, cid, [mid])
    insert_message(db, cid, ext_id="11", ts=11_000, is_me=1)
    clock.advance_ms(2 * MIN)
    assert (await _wait_host(client, w, known={}))["sessions"]["signals"] == []
    assert (await _session(ui, hub_id))["sub"]["pending"] == 0


async def test_signals_handle_only_host(client, ui, db, world):
    w = world
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    path = f"{API}/work/host/sessions/{hub_id}/signals"
    for token in (w["tok"], _triage(db)):
        r = await client.post(path, json={}, headers=_h(token))
        assert r.status_code == 403, r.text
    r = await ui.post(path)
    assert r.status_code == 403
    # Нет подписки — пусто; закрытая — 410; нет записи — 404.
    await _idle(client, w, hub_id)
    assert await _signals(client, w, hub_id) == {
        "sub_id": None,
        "project_id": w["p3"],
        "signals": [],
        "chan_signals": [],
    }
    assert (await ui.post(f"{API}/sessions/{hub_id}/close")).status_code == 200
    await _signals(client, w, hub_id, expect=410)
    await _signals(client, w, 999, expect=404)


async def test_delivery_refused_until_session_can_take(client, ui, db, clock, app, world):
    """П6: не `idle`, «усыпить», реплика владельца в очереди, остановка ядра — пусто и ничего не отмечено."""
    w = world
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    await _hsub(client, w, hub_id, mode="direct")
    await _sub(client, w, B, cwd=w["dir4"], role=CODER)
    msg_id = await _peer(client, w)

    def _signals_count():
        with db.reader() as conn:
            return conn.execute("SELECT signals FROM main.peer_msg WHERE id = ?", (msg_id,)).fetchone()[0]

    async def _empty():
        body = await _signals(client, w, hub_id)
        assert body["signals"] == [] and body["chan_signals"] == [] and _signals_count() == 0

    await _empty()  # спит
    await _host(client, w, f"sessions/{hub_id}/state", {"state": "running", **PROC})
    await _empty()  # ход идёт
    await _idle(client, w, hub_id)
    r = await ui.post(f"{API}/sessions/{hub_id}/say", {"text": "сначала это"})
    assert r.status_code == 200, r.text
    await _empty()  # реплика владельца раньше сигнала
    await _host(client, w, f"session-says/{r.json()['id']}/delivered", {})
    assert (await ui.post(f"{API}/sessions/{hub_id}/sleep")).status_code == 200
    await _empty()  # усыпляется
    await _host(client, w, f"sessions/{hub_id}/turn", {"state": "idle", "clear": ["sleep"]})
    app.state.notifier.close()
    await _empty()  # ядро останавливается
    with db.reader() as conn:
        assert held.get(conn, hub_id).signal_turn_ts is None


# --- 2.4, П11: `listening` ----------------------------------------------------------------------------------


async def test_listening_follows_host_wait(client, ui, db, clock, world):
    w = world
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    sub_id = (await _hsub(client, w, hub_id, mode="direct"))["sub"]["id"]
    assert (await _session(ui, hub_id))["listening"] is False  # хост ещё не ждал
    await _wait_host(client, w, known={})
    assert (await _session(ui, hub_id))["listening"] is True  # и у спящей: её поднимет сигнал
    rows = (await client.get(f"{API}/subs", headers=_h(w["tok"]))).json()["items"]
    assert [(row["id"], row["listening"]) for row in rows] == [(sub_id, True)]
    clock.advance_ms(held.HOST_STALE_MS + 1)
    assert (await _session(ui, hub_id))["listening"] is False
    rows = (await client.get(f"{API}/subs", headers=_h(w["tok"]))).json()["items"]
    assert [(row["id"], row["listening"]) for row in rows] == [(sub_id, False)]
    # Сессия без подписки не «слушает».
    other = (await _mk(ui, w))["id"]
    await _wait_host(client, w, known={})
    assert (await _session(ui, other))["listening"] is False


# --- 2.5, П3г: «Поднять» ------------------------------------------------------------------------------------


async def test_wake_request(client, ui, db, clock, world):
    w = world
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    path = f"{API}/sessions/{hub_id}/wake"
    assert (await client.post(path, json={}, headers=_h(w["host"]))).status_code == 403
    await _hsub(client, w, hub_id, mode="direct")
    await _sub(client, w, B, cwd=w["dir4"], role=CODER)
    await _peer(client, w)
    r = await ui.post(path)
    assert r.status_code == 200, r.text
    assert r.json()["session"]["id"] == hub_id
    rows = {row["id"]: row for row in (await _host(client, w, "sessions"))["items"]}
    asked = rows[hub_id]["wake_requested_ts"]
    assert asked == _now(clock)
    # Хост ставит и снимает причину `heavy`; уход в сон просьбу «поднять» не снимает, хост снимает её сам.
    row = await _host(client, w, f"sessions/{hub_id}/turn", {"state": "asleep", "wait_reason": "heavy"})
    assert row["session"]["wait_reason"] == "heavy" and row["session"]["wake_requested_ts"] == asked
    assert (await _session(ui, hub_id))["wait_reason"] == "heavy"
    row = await _host(
        client, w, f"sessions/{hub_id}/turn", {"state": "idle", "clear": ["wake"], "wait_reason": None}
    )
    assert row["session"]["wake_requested_ts"] is None and row["session"]["wait_reason"] is None
    # Начало хода снимает просьбу само.
    assert (await ui.post(path)).status_code == 200
    row = await _host(client, w, f"sessions/{hub_id}/turn", {"state": "running"})
    assert row["session"]["wake_requested_ts"] is None
    # Реплика в очереди — тоже повод; закрытая — 409.
    other = (await _mk(ui, w))["id"]
    assert (await ui.post(f"{API}/sessions/{other}/say", {"text": "привет"})).status_code == 200
    assert (await ui.post(f"{API}/sessions/{other}/wake")).status_code == 200
    assert (await ui.post(f"{API}/sessions/{other}/close")).status_code == 200
    assert (await ui.post(f"{API}/sessions/{other}/wake")).status_code == 409
    await _host(client, w, f"sessions/{hub_id}/turn", {"state": "idle", "clear": ["interrupt"]}, expect=422)
    await _host(client, w, f"sessions/{hub_id}/turn", {"state": "idle", "origin": "say"}, expect=422)


# --- П5: ход по сигналу, оборванный выключением ----------------------------------------------------------


async def test_signal_turn_lost_on_recover_and_stale(client, ui, db, clock, app, world):
    w = world
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    await _hsub(client, w, hub_id, mode="direct")
    await _sub(client, w, B, cwd=w["dir4"], role=CODER)

    def _rec():
        with db.reader() as conn:
            return held.get(conn, hub_id)

    async def _host_row():
        return {row["id"]: row for row in (await _host(client, w, "sessions"))["items"]}[hub_id]

    # Обычный ход по сигналу: выдача ставит отметку, обычный конец хода её снимает.
    await _peer(client, w)
    await _idle(client, w, hub_id)
    assert len((await _signals(client, w, hub_id))["signals"]) == 1
    assert _rec().signal_turn_ts == _now(clock)
    await _host(client, w, f"sessions/{hub_id}/turn", {"state": "running", "origin": "signal"})
    await _host(client, w, f"sessions/{hub_id}/state", {"state": "running", **PROC})  # подтверждение круга
    assert _rec().signal_turn_ts is not None
    await _host(client, w, f"sessions/{hub_id}/turn", {"state": "idle", "last_turn_ts": _now(clock)})
    assert _rec().signal_turn_ts is None and (await _host_row())["signal_lost"] is False

    # Выключение посреди хода по сигналу: `recover` ставит признак, сигнал уже отмечен выданным.
    clock.advance_ms(MIN)
    await _peer(client, w, text="второе")
    assert len((await _signals(client, w, hub_id))["signals"]) == 1
    await _host(client, w, f"sessions/{hub_id}/turn", {"state": "running", "origin": "signal"})
    await _host(client, w, "sessions/recover", {})
    rec = _rec()
    assert (rec.state, rec.signal_turn_ts, rec.signal_lost_ts) == ("asleep", None, _now(clock))
    assert (await _host_row())["signal_lost"] is True
    # Признак — повод для «Поднять»; хост подаёт постоянную строку и снимает его началом хода.
    assert (await ui.post(f"{API}/sessions/{hub_id}/wake")).status_code == 200
    await _idle(client, w, hub_id)
    row = await _host(
        client,
        w,
        f"sessions/{hub_id}/turn",
        {"state": "running", "origin": "signal", "clear": ["signal_lost", "wake"]},
    )
    assert row["session"]["signal_lost"] is False and _rec().signal_turn_ts == _now(clock)
    # Хост замолчал на 120 с посреди этого хода — признак снова.
    clock.advance_ms(held.HOST_STALE_MS + 1)
    from hub.core import hub_sessions

    assert db.write_sync(lambda conn: hub_sessions.stale_to_asleep(conn, now_ms=_now(clock))) == [hub_id]
    assert _rec().signal_lost_ts == _now(clock) and _rec().signal_turn_ts is None
    await _host(client, w, f"sessions/{hub_id}/turn", {"state": "asleep", "clear": ["signal_lost"]})

    # Усыпил сам владелец — признака нет: ход оборвал он.
    clock.advance_ms(MIN)
    await _peer(client, w, text="третье")
    await _idle(client, w, hub_id)
    assert len((await _signals(client, w, hub_id))["signals"]) == 1
    await _host(client, w, f"sessions/{hub_id}/turn", {"state": "running", "origin": "signal"})
    assert (await ui.post(f"{API}/sessions/{hub_id}/sleep")).status_code == 200
    await _host(client, w, f"sessions/{hub_id}/turn", {"state": "asleep", "clear": ["sleep"]})
    assert _rec().signal_lost_ts is None and _rec().signal_turn_ts is None

    # Запись во ввод не удалась (процесс умер сразу после выдачи) — тоже оборван.
    clock.advance_ms(MIN)
    await _peer(client, w, text="четвёртое")
    await _idle(client, w, hub_id)
    assert len((await _signals(client, w, hub_id))["signals"]) == 1
    await _host(client, w, f"sessions/{hub_id}/turn", {"state": "asleep"})
    assert _rec().signal_lost_ts == _now(clock)


# --- П12: подписки на беседы -------------------------------------------------------------------------------


async def test_chan_sub_of_hub_session_ends_on_archive_by_host_wait(client, ui, db, clock, world):
    w = world
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    r = await client.post(
        f"{API}/chan-subs",
        json={"channel_id": w["ch2"], "session": _hs(hub_id, cwd=w["dir3"])},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
    chan_sub_id = r.json()["sub"]["id"]
    clock.advance_ms(MIN)
    _ingest(db, w, [_ev("other", "301", _now(clock), text="привет")], clock)
    # Сессия только с подпиской на беседу тоже в разделе готовности.
    assert (await _wait_host(client, w, known={}))["sessions"]["signals"] == [
        {"id": hub_id, "ready": 1, "urgent": 0, "next_ms": None}
    ]
    assert (await _session(ui, hub_id))["sub"] is None
    db.write_sync(lambda conn: conn.execute("UPDATE main.channel SET archived = 1 WHERE id = ?", (w["ch2"],)))
    data = await _wait_host(client, w, known={hub_id: 1})  # следующий круг ожидания хоста
    assert data["sessions"]["signals"] == []
    with db.reader() as conn:
        row = conn.execute(
            "SELECT end_reason FROM main.session_chan_sub WHERE id = ?", (chan_sub_id,)
        ).fetchone()
    assert row == ("channel_gone",)
    await _idle(client, w, hub_id)
    assert (await _signals(client, w, hub_id))["chan_signals"] == []


async def test_revoked_token_sub_is_not_delivered(client, ui, db, clock, world):
    w = world
    tok, tok_id = _token(db, name="mcp:other")
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    await _hsub(client, w, hub_id, mode="direct", token=tok)
    await _sub(client, w, B, cwd=w["dir4"], role=CODER)
    await _peer(client, w)
    db.write_sync(lambda conn: security.revoke_api_token(conn, tok_id))
    assert (await _wait_host(client, w, known={}))["sessions"]["signals"] == []
    await _idle(client, w, hub_id)
    assert (await _signals(client, w, hub_id))["signals"] == []


# --- миграция ----------------------------------------------------------------------------------------------


def test_migration_0060_columns_and_heavy(db):
    with db.reader() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA main.table_info(hub_session)")}
    assert {
        "want_sub_json",
        "wake_requested_ts",
        "compact_requested_ts",
        "signal_turn_ts",
        "signal_lost_ts",
        "wait_reason",
    } <= columns

    def _insert(conn, reason):
        conn.execute(
            "INSERT INTO main.hub_session (project_id, engine, mode, workdir, state, created_ts, updated_ts, "
            "created_by, wait_reason) VALUES (1, 'claude', 'manual', '/tmp', 'asleep', 0, 0, 'ui', ?)",
            (reason,),
        )

    db.write_sync(lambda conn: conn.execute("INSERT INTO main.project (id, name) VALUES (1, 'п')"))
    for reason in ("slot", "elsewhere", "paused", "heavy", None):
        db.write_sync(lambda conn, reason=reason: _insert(conn, reason))
    with pytest.raises(sqlite3.IntegrityError):
        db.write_sync(lambda conn: _insert(conn, "прочее"))


def test_waiting_counts_nothing_for_session_without_subs(db, world):
    with db.reader() as conn:
        assert held_signals.snapshot(conn, now_ms=0, presence=[]) == []


async def test_wake_asleep_session_with_nothing_pending(client, ui, db, clock, world):
    """10.10 (владелец): «Поднять» у спящей без ждущего поднимает — признак постоянной строки; поднятая — 409."""
    w = world
    hub_id = (await _mk(ui, w, role=ROLE))["id"]
    await _host(client, w, f"sessions/{hub_id}/turn", {"state": "asleep"})
    assert (await ui.post(f"{API}/sessions/{hub_id}/wake")).status_code == 200
    rows = {row["id"]: row for row in (await _host(client, w, "sessions"))["items"]}
    assert rows[hub_id]["wake_requested_ts"] == _now(clock) and rows[hub_id]["signal_lost"] is True
    await _host(client, w, f"sessions/{hub_id}/turn", {"state": "idle", "clear": ["wake", "signal_lost"]})
    r = await ui.post(f"{API}/sessions/{hub_id}/wake")
    assert r.status_code == 409 and r.json()["detail"] == "поднимать не на что"


async def test_sub_of_other_machine_token_is_stale_for_this_token(client, ui, db, world):
    """Ф33 (19.10): подписка сессии хаба оформлена токеном профиля `all` другой площадки — для токена этой
    машины запись говорит `sub_stale`, сервер MCP оформляет подписку заново, и инструменты её находят."""
    w = world
    other, _other_id = _token(db, name="mcp:all")
    hub_id = (await _mk(ui, w, role=ROLE, want_sub={"mode": "direct"}))["id"]
    old = (await _hsub(client, w, hub_id, mode="direct", token=other))["sub"]["id"]

    async def record(token) -> dict:
        r = await client.get(f"{API}/sessions/{hub_id}", headers=_h(token))
        assert r.status_code == 200, r.text
        return r.json()["session"]

    assert (await record(other))["sub_stale"] is False
    assert (await record(w["tok"]))["sub_stale"] is True
    assert (await _session(ui, hub_id))["sub_stale"] is False  # окну владельца токен не важен
    r = await client.get(
        f"{API}/peer-msgs", params={"pid": -hub_id, "proc_start": 0, "session_id": f"hub:{hub_id}"},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 409, r.text
    new = (await _hsub(client, w, hub_id, mode="direct"))["sub"]["id"]
    assert new != old
    assert (await record(w["tok"]))["sub_stale"] is False
    r = await client.get(
        f"{API}/peer-msgs", params={"pid": -hub_id, "proc_start": 0, "session_id": f"hub:{hub_id}"},
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
