"""Ф52-2 (docs/architecture.md): проект слышит «Неразобранное»
выбранных аккаунтов — только СИГНАЛ подпискам проекта; граница чтения не меняется.

- запись права: только вход владельца (интерфейс) и админ-сокет (CLI); bearer — отказ; неизвестный аккаунт — 422;
- сигнал сообщений (`messages` / `personal`) и карточек: без права — ничего (регресс), с правом — подписке
  проекта, слышащего аккаунт; фильтр аккаунтов, ключи, заглушённые, свои сообщения, `direct`;
- два проекта слышат один аккаунт — сигнал обоим;
- годность при выдаче: беседу разложили — `moved`, право снято — не выдаётся;
- подписка сессии OpenCode — тем же путём;
- правка по ревью: право действует только для подписки, чей токен САМ видит «Неразобранное» (профиль на
  все проекты со скоупом `organize`); проектному токену — ни сигнала, ни ключей, ни поля в ответе подписки;
  обычные сигналы бесед проекта у него прежние.

Тест П8 (заявка на перенос устарела) — `test_f52_move_stale.py`.

Обвязка — `test_f26_multi_subs.py`; в мире два проекта (P и Q) и два аккаунта (X и Y) с беседами без
проекта."""

import pytest
from httpx import ASGITransport, AsyncClient

from hub import cli as hub_cli
from hub import sub_wait
from hub.core import chan_subs, ingest, security, subs, unsorted
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret

from . import test_hub_sessions as base
from .conftest import insert_account, insert_channel, insert_project, make_bearer_token
from .test_f26_multi_subs import (
    A,
    B,
    C,
    _card,
    _card_signals,
    _ev,
    _h,
    _msg_id,
    _msg_signals,
    _now,
    _token,
)

ui = base.ui

API = "/api/v1"
ADMIN_SECRET = ADMIN_PREFIX + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHI"
OC = (7171, 111_000, "ses_f52unsorted0001")


@pytest.fixture
def procs(monkeypatch):
    table = {A[0]: A[1], B[0]: B[1], C[0]: C[1], OC[0]: OC[1]}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


@pytest.fixture
def world(db, tmp_path):
    dirs = {}
    for name in ("p", "q"):
        dirs[name] = tmp_path / name
        dirs[name].mkdir()
    p = insert_project(db, name="Hooli", workdir=str(dirs["p"]))
    q = insert_project(db, name="Umbrella", workdir=str(dirs["q"]))
    for pid in (p, q):
        db.write_sync(lambda conn, pid=pid: subs.set_project_subs(conn, pid, True, now_ms=0))
    x = insert_account(db, source="slack")
    y = insert_account(db, source="slack")
    ch = {
        "UX": insert_channel(db, x, project_id=None, ext_id="UX", kind="dm"),
        "GX": insert_channel(db, x, project_id=None, ext_id="GX", kind="group"),
        "MX": insert_channel(db, x, project_id=None, ext_id="MX", kind="group", muted=1),
        "UY": insert_channel(db, y, project_id=None, ext_id="UY", kind="dm"),
    }
    return {
        "P": p,
        "Q": q,
        "dirs": {"P": str(dirs["p"]), "Q": str(dirs["q"])},
        "X": x,
        "Y": y,
        "ch": ch,
        # основной случай: токен, который сам видит «Неразобранное» (все проекты + `organize`, профиль `all`)
        "tok": _token(db, scopes=("read", "draft", "organize")),
        "triage": _token(db, name="agent:triage", kind="background", scopes=("read", "triage")),
    }


def _grant(db, project_id: int, accounts: list[int]) -> None:
    assert db.write_sync(lambda conn: unsorted.set_project(conn, project_id, accounts)) == sorted(accounts)


async def _sub(client, w, who, project="P", *, expect=200, kind="interactive", **extra):
    pid, start, sid = who
    session = {
        "pid": pid,
        "proc_start": start,
        "session_id": sid,
        "cwd": w["dirs"][project],
        "kind": kind,
        "name": None,
        "name_source": None,
    }
    body: dict = {"session": session, **extra}
    if extra.get("mode") == "personal":
        body["mode"], body["msg_filter"] = "messages", "personal"
    r = await client.post(f"{API}/subs", json=body, headers=_h(w["tok"]))
    assert r.status_code == expect, r.text
    return r.json()


async def _wait(client, w, sub_id, who) -> list[dict]:
    pid, start, sid = who
    r = await client.post(
        f"{API}/subs/{sub_id}/wait",
        json={
            "session": {"pid": pid, "proc_start": start, "session_id": sid},
            "waiter": "0123456789abcdef",
            "timeout_s": 1,
        },
        headers=_h(w["tok"]),
    )
    assert r.status_code == 200, r.text
    return r.json()["signals"]


def _ing(db, w, acc: str, events, now_ms) -> None:
    result = db.write_sync(
        lambda conn: ingest.apply_batch(conn, account_id=w[acc], events=events, cursor=None, now_ms=now_ms)
    )
    assert result.dead == 0, result


def _move(db, channel_id: int, project_id: int | None) -> None:
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET project_id = ? WHERE id = ?", (project_id, channel_id)
        )
    )


# --- запись права -----------------------------------------------------------------------------------------


async def test_right_is_written_only_by_owner(client, ui, db, clock, world, app):  # noqa: F811
    w = world
    p, x, y = w["P"], w["X"], w["Y"]
    with db.reader() as conn:
        assert conn.execute("SELECT unsorted_accounts FROM main.project WHERE id = ?", (p,)).fetchone() == (
            None,
        )
    path = f"{API}/projects/{p}"

    # bearer (и «на все проекты» с organize, и проектный) — отказ, право не записано
    org = make_bearer_token(db, scopes=("read", "draft", "organize"), name="mcp:org")
    own = make_bearer_token(db, scopes=("read", "draft"), project_ids=[p], name="mcp:p")
    for token in (org, own, w["tok"]):
        r = await client.patch(path, json={"unsorted_accounts": [x]}, headers=_h(token))
        assert r.status_code in (401, 403), r.text
    with db.reader() as conn:
        assert unsorted.project_accounts(conn, p) == []

    # Владелец: список хранится уникальным и по возрастанию, `GET /projects` отдаёт его входу владельца
    r = await ui.c.patch(path, json={"unsorted_accounts": [y, x, y]}, headers=ui.headers)
    assert r.status_code == 200, r.text
    with db.reader() as conn:
        assert unsorted.project_accounts(conn, p) == sorted([x, y])
        assert unsorted.hears(conn, p, x) and not unsorted.hears(conn, w["Q"], x)
    listed = {row["id"]: row for row in (await ui.get(f"{API}/projects")).json()}
    assert listed[p]["unsorted_accounts"] == sorted([x, y])
    assert listed[w["Q"]]["unsorted_accounts"] == []
    # bearer поле не получает
    for token in (org, own):
        rows = (await client.get(f"{API}/projects", headers=_h(token))).json()
        assert rows and all("unsorted_accounts" not in row for row in rows)

    # отказы формы: неизвестный аккаунт, null, не числа — 422, право прежнее
    r = await ui.c.patch(path, json={"unsorted_accounts": [x, 999]}, headers=ui.headers)
    assert r.status_code == 422 and r.json()["detail"] == "unsorted_accounts: нет аккаунтов 999"
    for bad in (None, [0], [True], ["1"], "8", [x] * 21):
        r = await ui.c.patch(path, json={"unsorted_accounts": bad}, headers=ui.headers)
        assert r.status_code == 422, (bad, r.text)
    r = await ui.c.patch(f"{API}/projects/987", json={"unsorted_accounts": [x]}, headers=ui.headers)
    assert r.status_code == 404
    with db.reader() as conn:
        assert unsorted.project_accounts(conn, p) == sorted([x, y])

    # пустой список — снять (столбец снова NULL)
    r = await ui.c.patch(path, json={"unsorted_accounts": []}, headers=ui.headers)
    assert r.status_code == 200
    with db.reader() as conn:
        assert conn.execute("SELECT unsorted_accounts FROM main.project WHERE id = ?", (p,)).fetchone() == (
            None,
        )

    # админ-сокет (CLI `hub project set N --unsorted-accounts …`)
    internal = create_internal_app(
        db,
        clock=clock,
        notifier=app.state.notifier,
        admin_key_hash=hash_secret(ADMIN_SECRET),
        sub_waiters=app.state.sub_waiters,
        chan_sub_waiters=app.state.chan_sub_waiters,
    )
    admin = {"Authorization": f"Bearer {ADMIN_SECRET}"}
    async with AsyncClient(transport=ASGITransport(app=internal), base_url="http://hub-internal") as c:
        url = f"/internal/v1/admin/projects/{p}"
        assert (await c.patch(url, json={"unsorted_accounts": [x]})).status_code in (401, 403)
        r = await c.patch(url, json={"unsorted_accounts": [x]}, headers=admin)
        assert r.status_code == 200 and r.json()["unsorted_accounts"] == [x]
        r = await c.patch(url, json={"unsorted_accounts": [999]}, headers=admin)
        assert r.status_code == 422 and "нет аккаунтов 999" in r.json()["detail"]
        rows = {row["id"]: row for row in (await c.get("/internal/v1/admin/projects", headers=admin)).json()}
        assert rows[p]["unsorted_accounts"] == [x]
        r = await c.patch(url, json={"unsorted_accounts": []}, headers=admin)
        assert r.status_code == 200 and r.json()["unsorted_accounts"] == []


def test_cli_parses_accounts():
    assert hub_cli._parse_unsorted_accounts("8,5") == [8, 5]
    assert hub_cli._parse_unsorted_accounts(" 8 , 5 ") == [8, 5]
    assert hub_cli._parse_unsorted_accounts("off") == []
    for bad in ("", "8,", "a", "0", "-1", "8;5"):
        with pytest.raises(SystemExit):
            hub_cli._parse_unsorted_accounts(bad)


def test_broken_column_means_not_heard():
    for raw in (None, "", "{", '{"only": [1]}', '["1"]', "[true]", "[0]", "8"):
        assert unsorted.parse(raw) == [], raw
    assert unsorted.parse("[8, 5, 8]") == [5, 8]


# --- сообщения --------------------------------------------------------------------------------------------


async def test_without_right_no_signal(client, db, world, procs, clock):
    """Регресс: права нет — беседа без проекта не оставляет подписке ни сигнала, ни строки отсева."""
    w = world
    await _sub(client, w, A, mode="messages", msg_filter="all")
    await _sub(client, w, B, "Q")
    now = _now(clock)
    _ing(db, w, "X", [_ev("UX", "1.0", now, kind="dm")], now)
    await _card(client, w, w["ch"]["UX"], [_msg_id(db, w["ch"]["UX"], "1.0")])
    assert _msg_signals(db) == [] and _card_signals(db) == []


async def test_messages_signal_with_right(client, db, world, procs, clock):
    w = world
    ch, p, x = w["ch"], w["P"], w["X"]
    _grant(db, p, [x])
    s1 = (await _sub(client, w, A, mode="messages", msg_filter="all"))["sub"]["id"]
    now = _now(clock)
    _ing(db, w, "X", [_ev("UX", "0.5", now - 1, kind="dm", is_me=True)], now)  # своё сообщение владельца — нет
    _ing(db, w, "X", [_ev("UX", "1.0", now, kind="dm")], now)
    _ing(db, w, "X", [_ev("MX", "1.0", now + 2)], now)  # заглушённая без ключей — нет
    _ing(db, w, "Y", [_ev("UY", "1.0", now + 3, kind="dm")], now)  # аккаунт Y проект не слышит
    assert _msg_signals(db) == [(None, s1, ch["UX"], "pending", None, "dm", None)]
    # выдача: числа и пометка `unsorted`; в строке клиента — постоянный текст с номерами
    (signal,) = await _wait(client, w, s1, A)
    assert signal["unsorted"] is True
    assert (signal["channel_id"], signal["account_id"], signal["kind"]) == (ch["UX"], x, "messages")
    _sub_id, _project, valid = sub_wait.valid_signals({"sub_id": s1, "project_id": p, "signals": [signal]})
    text = sub_wait.signals_text(s1, p, valid, "cmd")
    assert (
        f"неразобранное (аккаунт {x}): беседа #{ch['UX']} · личка · новых 1, последнее msg "
        f"{signal['message_id']} — hub_read(channel_id={ch['UX']}); по твоему проекту — "
        f"hub_move_channel(channel_id={ch['UX']}, project_id={p})"
    ) in text.splitlines()


async def test_personal_keys_muted_and_account_filter(client, db, world, procs, clock):
    w = world
    ch, x, y = w["ch"], w["X"], w["Y"]
    _grant(db, w["P"], [x, y])
    keys = {"keywords": ["заказ"], "batch_min": 0}
    s1 = (await _sub(client, w, A, mode="personal", **keys, accounts=[x]))["sub"]["id"]
    s2 = (await _sub(client, w, B, mode="personal", accounts_except=[x]))["sub"]["id"]
    now = _now(clock)
    _ing(db, w, "X", [_ev("GX", "1.0", now, text="есть заказ")], now)  # ключ — только слышащей X
    _ing(db, w, "X", [_ev("GX", "2.0", now + 1, text="ни о чём")], now)  # без ключа — отсев у неё же
    _ing(db, w, "X", [_ev("MX", "1.0", now + 2, text="заказ")], now)  # заглушённая с ключом — слышна
    _ing(db, w, "Y", [_ev("UY", "1.0", now + 3, kind="dm")], now)  # личка аккаунта Y — только второй
    assert _msg_signals(db) == [
        (None, s1, ch["GX"], "pending", None, "keyword", ["заказ"]),
        (None, s1, ch["GX"], "skipped", "no_keyword", None, None),
        (None, s1, ch["MX"], "pending", None, "keyword", ["заказ"]),
        (None, s2, ch["UY"], "pending", None, "dm", None),
    ]


async def test_two_projects_hear_one_account(client, db, world, procs, clock):
    w = world
    ch, x = w["ch"], w["X"]
    _grant(db, w["P"], [x])
    _grant(db, w["Q"], [x])
    s1 = (await _sub(client, w, A, "P", mode="messages", msg_filter="all"))["sub"]["id"]
    s2 = (await _sub(client, w, B, "Q", mode="messages", msg_filter="all"))["sub"]["id"]
    c1 = (await _sub(client, w, C, "Q"))["sub"]["id"]  # карточки второго проекта
    now = _now(clock)
    _ing(db, w, "X", [_ev("UX", "1.0", now, kind="dm")], now)
    assert [(r[1], r[2], r[3]) for r in _msg_signals(db)] == [
        (s1, ch["UX"], "pending"),
        (s2, ch["UX"], "pending"),
    ]
    item = await _card(client, w, ch["UX"], [_msg_id(db, ch["UX"], "1.0")])
    assert _card_signals(db) == [(c1, item, "pending", None)]
    with db.reader() as conn:  # в строке сигнала карточки — проект подписки (столбец NOT NULL)
        assert conn.execute("SELECT project_id FROM main.session_signal").fetchall() == [(w["Q"],)]
    # заглушённый проект сообщений не слышит
    db.write_sync(lambda conn: conn.execute("UPDATE main.project SET muted = 1 WHERE id = ?", (w["Q"],)))
    _ing(db, w, "X", [_ev("GX", "1.0", now + 1)], now)
    assert [(r[1], r[2]) for r in _msg_signals(db) if r[2] == ch["GX"]] == [(s1, ch["GX"])]


async def test_moved_before_delivery(client, db, world, procs, clock):
    """Беседу разложили в другой проект до выдачи — у слышавшего её как «Неразобранное» сигнал `moved`;
    у нового проекта беседы тот же сигнал уходит обычным (без пометки)."""
    w = world
    ch, x = w["ch"], w["X"]
    _grant(db, w["P"], [x])
    _grant(db, w["Q"], [x])
    s1 = (await _sub(client, w, A, "P", mode="messages", msg_filter="all"))["sub"]["id"]
    s2 = (await _sub(client, w, B, "Q", mode="messages", msg_filter="all"))["sub"]["id"]
    now = _now(clock)
    _ing(db, w, "X", [_ev("UX", "1.0", now, kind="dm")], now)
    _move(db, ch["UX"], w["Q"])
    with db.reader() as conn:  # счёт готового хосту — тем же определением
        assert chan_subs.valid_state(conn, now_ms=now, project_sub=subs.get_sub(conn, s1))[0] == 0
        assert chan_subs.valid_state(conn, now_ms=now, project_sub=subs.get_sub(conn, s2))[0] == 1
    assert await _wait(client, w, s1, A) == []
    (signal,) = await _wait(client, w, s2, B)
    assert "unsorted" not in signal and signal["channel_id"] == ch["UX"]
    assert [(r[1], r[3], r[4]) for r in _msg_signals(db)] == [
        (s1, "skipped", "moved"),
        (s2, "delivered", None),
    ]


async def test_right_revoked_before_delivery(client, db, world, procs, clock):
    w = world
    ch, x = w["ch"], w["X"]
    _grant(db, w["P"], [x])
    s1 = (await _sub(client, w, A, mode="messages", msg_filter="all"))["sub"]["id"]
    c1 = (await _sub(client, w, B))["sub"]["id"]
    now = _now(clock)
    _ing(db, w, "X", [_ev("UX", "1.0", now, kind="dm")], now)
    item = await _card(client, w, ch["UX"], [_msg_id(db, ch["UX"], "1.0")])
    assert _card_signals(db) == [(c1, item, "pending", None)]
    _grant(db, w["P"], [])
    with db.reader() as conn:
        assert subs.valid_cards(conn, subs.get_sub(conn, c1), visible_projects=None, presence=[]) == 0
    assert await _wait(client, w, s1, A) == []
    assert await _wait(client, w, c1, B) == []
    assert [(r[1], r[3], r[4]) for r in _msg_signals(db)] == [(s1, "skipped", "moved")]
    assert _card_signals(db) == [(c1, item, "skipped", "moved")]


# --- карточки ---------------------------------------------------------------------------------------------


async def test_card_signal_with_right(client, db, world, procs, clock):
    w = world
    ch, p, x, y = w["ch"], w["P"], w["X"], w["Y"]
    _grant(db, p, [x])
    c1 = (await _sub(client, w, A))["sub"]["id"]
    c2 = (await _sub(client, w, B, accounts=[y]))["sub"]["id"]  # фильтр подписки — аккаунт X не слышит
    now = _now(clock)
    _ing(db, w, "X", [_ev("UX", "1.0", now, kind="dm")], now)
    _ing(db, w, "Y", [_ev("UY", "1.0", now, kind="dm")], now)
    item = await _card(client, w, ch["UX"], [_msg_id(db, ch["UX"], "1.0")])
    await _card(client, w, ch["UY"], [_msg_id(db, ch["UY"], "1.0")])  # аккаунт Y проект не слышит
    assert _card_signals(db) == [(c1, item, "pending", None)]
    with db.reader() as conn:
        assert subs.valid_cards(conn, subs.get_sub(conn, c1), visible_projects=None, presence=[]) == 1
    (signal,) = await _wait(client, w, c1, A)
    assert signal == {
        "item_id": item,
        "channel_id": ch["UX"],
        "account_id": x,
        "label": "reply",
        "msg_count": 1,
        "unsorted": True,
    }
    assert await _wait(client, w, c2, B) == []
    _sub_id, _project, valid = sub_wait.valid_signals({"sub_id": c1, "project_id": p, "signals": [signal]})
    text = sub_wait.signals_text(c1, p, valid, "cmd")
    assert (
        f"неразобранное (аккаунт {x}): беседа #{ch['UX']} · карточка #{item} · ждёт ответа · сообщений 1 "
        f"— hub_read(channel_id={ch['UX']}); по твоему проекту — "
        f"hub_move_channel(channel_id={ch['UX']}, project_id={p})"
    ) in text.splitlines()


def test_old_client_fields_and_plain_lines_unchanged():
    """Пометка — только буквальное `true`; без неё строки прежние (протокол ожидания не поднимался)."""
    raw = {"item_id": 3, "channel_id": 7, "account_id": 8, "label": "reply", "msg_count": 2}
    for junk in (1, "true", "да", None):
        body = {"sub_id": 1, "project_id": 5, "signals": [{**raw, "unsorted": junk}]}
        _s, _p, valid = sub_wait.valid_signals(body)
        assert valid == [raw]
    text = sub_wait.signals_text(1, 5, [raw], "cmd")
    assert "карточка #3 · ждёт ответа · беседа #7 · аккаунт 8 · сообщений 2" in text.splitlines()
    assert "неразобранное" not in text


# --- direct и OpenCode ------------------------------------------------------------------------------------


async def test_direct_does_not_hear(client, db, world, procs, clock):
    w = world
    ch, x = w["ch"], w["X"]
    _grant(db, w["P"], [x])
    body = await _sub(client, w, A, mode="direct", role="менеджер")
    assert "unsorted_accounts" not in body
    now = _now(clock)
    _ing(db, w, "X", [_ev("UX", "1.0", now, kind="dm")], now)
    await _card(client, w, ch["UX"], [_msg_id(db, ch["UX"], "1.0")])
    assert _msg_signals(db) == [] and _card_signals(db) == []


async def test_opencode_sub_same_path(client, db, world, procs, clock):
    w = world
    ch, x = w["ch"], w["X"]
    _grant(db, w["P"], [x])
    body = await _sub(client, w, OC, kind="opencode", mode="messages", msg_filter="all")
    assert body["unsorted_accounts"] == [x]  # ответ подписки называет право (текст `hub_subscribe`)
    s1 = body["sub"]["id"]
    c1 = (await _sub(client, w, (OC[0], OC[1], "ses_f52unsorted0002"), kind="opencode"))["sub"]["id"]
    now = _now(clock)
    _ing(db, w, "X", [_ev("UX", "1.0", now, kind="dm")], now)
    item = await _card(client, w, ch["UX"], [_msg_id(db, ch["UX"], "1.0")])
    (signal,) = await _wait(client, w, s1, OC)
    assert signal["unsorted"] is True and signal["channel_id"] == ch["UX"]
    (card,) = await _wait(client, w, c1, (OC[0], OC[1], "ses_f52unsorted0002"))
    assert card["unsorted"] is True and card["item_id"] == item


async def test_subscribe_answer_without_right_has_no_field(client, db, world, procs):
    assert "unsorted_accounts" not in await _sub(client, world, A)


# --- правка по ревью: токен, который «Неразобранное» не видит ---------------------------------------------


def _blind_tokens(db, w) -> dict:
    """Токены, которые сами «Неразобранное» не видят: проектный и «все проекты» без `organize`."""
    return {
        "own": make_bearer_token(db, scopes=("read", "draft"), project_ids=[w["P"]], name="mcp:p"),
        "plain": _token(db, name="mcp:plain"),
    }


@pytest.mark.parametrize("which", ["own", "plain"])
async def test_blind_token_gets_no_unsorted_signal_and_no_keys(client, db, world, procs, clock, which):
    """Находка ревью: подбором `keywords` проектный токен прощупывал беседы, которые читать не может.
    Теперь по беседе без проекта его подписке не пишется ничего — ни сигнала, ни ключей, ни строки отсева,
    ни карточки; ответ подписки право не называет. Сигналы бесед СВОЕГО проекта — прежние (регресс)."""
    w = world
    ch, p, x = w["ch"], w["P"], w["X"]
    _grant(db, p, [x])
    own_ch = insert_channel(db, x, project_id=p, ext_id="PX", kind="group")
    seeing = w["tok"]
    w["tok"] = _blind_tokens(db, w)[which]
    body = await _sub(client, w, A, mode="personal", keywords=["зарплата", "пароль"], batch_min=0)
    assert "unsorted_accounts" not in body
    s1 = body["sub"]["id"]
    c1 = (await _sub(client, w, B))["sub"]["id"]
    now = _now(clock)
    _ing(db, w, "X", [_ev("GX", "1.0", now, text="моя зарплата выросла")], now)
    _ing(db, w, "X", [_ev("GX", "2.0", now + 1, text="ни о чём")], now)
    _ing(db, w, "X", [_ev("UX", "1.0", now + 2, kind="dm")], now)
    await _card(client, w, ch["UX"], [_msg_id(db, ch["UX"], "1.0")])
    assert _msg_signals(db) == [] and _card_signals(db) == []
    assert await _wait(client, w, s1, A) == []
    assert await _wait(client, w, c1, B) == []
    if which == "own":  # граница чтения на месте: беседу этот токен не читает
        r = await client.get(f"{API}/channels/{ch['GX']}/messages", headers=_h(w["tok"]))
        assert r.status_code in (403, 404)
    # регресс: беседа своего проекта — сигнал с ключом и карточка, как раньше
    _ing(db, w, "X", [_ev("PX", "1.0", now + 3, text="пароль от стенда")], now)
    item = await _card(client, w, own_ch, [_msg_id(db, own_ch, "1.0")])
    assert _msg_signals(db) == [(None, s1, own_ch, "pending", None, "keyword", ["пароль"])]
    assert _card_signals(db) == [(c1, item, "pending", None)]
    (signal,) = await _wait(client, w, s1, A)
    assert signal["keys"] == ["пароль"] and "unsorted" not in signal
    (card,) = await _wait(client, w, c1, B)
    assert card["item_id"] == item and "unsorted" not in card
    # рядом подписка видящего токена на тот же проект право получает
    w["tok"] = seeing
    assert (await _sub(client, w, C, mode="messages", msg_filter="all"))["unsorted_accounts"] == [x]


async def test_token_lost_sight_before_delivery(client, db, world, procs, clock):
    """Проверка и при выдаче: сигнал записан видящему токену, до выдачи токен перестал видеть
    «Неразобранное» (сняли `organize`) — не выдаётся, `moved`; счётчики хосту — тем же определением."""
    w = world
    ch, x = w["ch"], w["X"]
    _grant(db, w["P"], [x])
    s1 = (await _sub(client, w, A, mode="personal", keywords=["заказ"], batch_min=0))["sub"]["id"]
    c1 = (await _sub(client, w, B))["sub"]["id"]
    now = _now(clock)
    _ing(db, w, "X", [_ev("GX", "1.0", now, text="есть заказ")], now)
    _ing(db, w, "X", [_ev("UX", "1.0", now + 1, kind="dm")], now)
    item = await _card(client, w, ch["UX"], [_msg_id(db, ch["UX"], "1.0")])
    assert len(_msg_signals(db)) == 2 and _card_signals(db) == [(c1, item, "pending", None)]
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.api_token SET scopes = 'read draft' WHERE name = 'mcp:all' AND kind = 'interactive'"
        )
    )
    with db.reader() as conn:
        assert not unsorted.token_sees(conn, subs.get_sub(conn, s1).api_token_id)
        assert chan_subs.valid_state(conn, now_ms=now + 5, project_sub=subs.get_sub(conn, s1))[0] == 0
        assert subs.valid_cards(conn, subs.get_sub(conn, c1), visible_projects=None, presence=[]) == 0
    assert await _wait(client, w, s1, A) == []
    assert await _wait(client, w, c1, B) == []
    assert sorted((r[3], r[4]) for r in _msg_signals(db)) == [("skipped", "moved"), ("skipped", "moved")]
    assert _card_signals(db) == [(c1, item, "skipped", "moved")]


def test_token_sees_rule(db):
    """`unsorted.token_sees` — то же правило, что `Principal.sees_unsorted`; нет строки токена — нет."""

    def _id(**kw):
        return db.write_sync(
            lambda conn: security.create_api_token(conn, expires_ts=None, kind="interactive", **kw)
        ).token_id

    seeing = _id(name="a", scopes=["read", "organize"], project_ids=None)
    plain = _id(name="b", scopes=["read", "draft"], project_ids=None)
    own = _id(name="c", scopes=["read", "draft"], project_ids=[1])
    with db.reader() as conn:
        assert unsorted.token_sees(conn, seeing)
        for token_id in (plain, own, None, 10_000):
            assert not unsorted.token_sees(conn, token_id), token_id
