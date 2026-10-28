"""Ф63-1 (docs/architecture.md П1–П7): правила проекта для входящих —
условие -> `wake` + `attention`.

- `inbound_rules.match` — таблица случаев (поддомены, адрес, ключи темы и текста, минус-слова, виды, трекеры);
- проверка полей: ключ до 6 слов (у подписок — по-прежнему 3), обязательные условие и действие, пределы;
- ингест: тема письма в событии, `rule_hits`/`rule_attention`, правило позже — старые не трогает, правка не
  пересчитывает, беседы трекеров и чужой проект не задевает, счётчик;
- внимание: рассылка + `react` -> «Нужна реакция»; ручной режим и заглушка владельца сильнее; `info` не гасит
  личку и прямое упоминание в чате, письмо «мне в Кому» — гасит;
- `wake`: подписка роли «сообщения» — сигнал сразу, мимо пачки и «лично мне», с номером правила; фильтр
  аккаунтов; заглушённая беседа — будит, заглушённый проект — нет; подписка «карточки» — нет;
- ручки: UI и сессия с ролью своего проекта, чужой проект — 403, без роли — 403, предел 50, аудит, чтение;
- вывод: `hub sub wait` («правило #N»), MCP `hub_read` («(правило #N: название)» внутри данных)."""

import json

import pytest

from hub import sub_wait
from hub.connectors.mail import convert as mail_convert
from hub.core import attention, chan_subs, inbound_rules, ingest, keywords, queries, security, subs
from hub.core.events import parse_event
from hub.mcp_server import format as fmt

from . import test_f26_multi_subs as f26
from .conftest import csrf_headers, insert_account, insert_channel, insert_project, login_via_code
from .test_f26_multi_subs import A, B, C, _h, _now, _session, _sub, _wait

procs = f26.procs
world = f26.world

UI = security.Principal(token_id=0, kind="ui", scopes=frozenset({"read"}), project_ids=None)
PHRASE = "новое сообщение по заказу"


# --- match: таблица -------------------------------------------------------------------------------------


def _rule(match: dict, action: dict | None = None, rule_id: int = 1, project_id: int = 1):
    return inbound_rules._compile(  # noqa: SLF001
        rule_id, project_id, json.dumps(match), json.dumps(action or {"wake": ["менеджер"]})
    )


def _ctx(kind="mail_thread", sender="no_reply@mail.example.com", subject=None, text=None, account=6):
    return inbound_rules.MsgCtx(account, kind, sender, subject, text)


@pytest.mark.parametrize(
    ("match", "ctx", "hit"),
    [
        # отправитель: домен совпадает с поддоменом, чужой домен и «хвост имени» — нет
        ({"from": ["example.com"]}, _ctx(), True),
        ({"from": ["mail.example.com"]}, _ctx(), True),
        ({"from": ["example.com"]}, _ctx(sender="x@badexample.com"), False),
        ({"from": ["example.com"]}, _ctx(sender="x@example.com.evil.ru"), False),
        ({"from": ["no_reply@mail.example.com"]}, _ctx(), True),
        ({"from": ["other@mail.example.com"]}, _ctx(), False),
        # `from` — только почта
        ({"from": ["example.com"]}, _ctx(kind="dm", sender=None), False),
        # тема — ключи по началу слова, фраза до 6 слов
        ({"subject": [PHRASE]}, _ctx(subject="Новое сообщение по заказу «Сайт»"), True),
        ({"subject": ["нов сообщ по заказ"]}, _ctx(subject="Новые сообщения по заказам"), True),
        ({"subject": [PHRASE]}, _ctx(subject="Сообщение по заказу"), False),
        # текст; тема И текст вместе
        ({"text": ["оплат"]}, _ctx(text="Ждём оплату"), True),
        ({"subject": ["заказ"], "text": ["оплат"]}, _ctx(subject="заказ 1", text="привет"), False),
        ({"subject": ["заказ"], "text": ["оплат"]}, _ctx(subject="заказ 1", text="оплата"), True),
        # минус-слово — и по теме, и по тексту
        ({"subject": ["заказ"], "exclude": ["отмен"]}, _ctx(subject="заказ отменён"), False),
        ({"subject": ["заказ"], "exclude": ["отмен"]}, _ctx(subject="заказ", text="отмена"), False),
        # аккаунты и виды
        ({"subject": ["заказ"], "accounts": [6]}, _ctx(subject="заказ"), True),
        ({"subject": ["заказ"], "accounts": [7]}, _ctx(subject="заказ"), False),
        ({"text": ["заказ"], "kinds": ["dm"]}, _ctx(kind="group", sender=None, text="заказ"), False),
        ({"text": ["заказ"], "kinds": ["group"]}, _ctx(kind="group", sender=None, text="заказ"), True),
        # беседы трекеров — никогда, даже без `kinds`
        ({"text": ["заказ"]}, _ctx(kind="issue", sender=None, text="заказ"), False),
        ({"text": ["заказ"]}, _ctx(kind="card", sender=None, text="заказ"), False),
        # без from/subject/text — не срабатывает (страховка)
        ({"accounts": [6]}, _ctx(), False),
    ],
)
def test_match_table(match, ctx, hit):
    assert inbound_rules.match(_rule(match), ctx) is hit


def test_normalize_words_max_and_required_parts():
    form = inbound_rules.normalize(
        {
            "title": "  заказы \n площадки ",
            "from": ["@Example.COM"],
            "subject": [PHRASE],
            "wake": ["Менеджер"],
        }
    )
    assert form["title"] == "заказы площадки"
    assert form["match"] == {"from": ["example.com"], "subject": [PHRASE]}
    assert form["action"] == {"wake": ["Менеджер"]}
    six = "раз два три четыре пять шесть"
    assert inbound_rules.normalize({"title": "t", "text": [six], "attention": "react"})["match"]["text"] == [
        six
    ]
    with pytest.raises(inbound_rules.RuleError, match=r"text 1: больше 6 слов"):
        inbound_rules.normalize({"title": "t", "text": [six + " семь"], "attention": "react"})
    # у подписок по-прежнему 3
    with pytest.raises(keywords.Invalid, match="больше 3 слов"):
        keywords.normalize([PHRASE], 10)
    with pytest.raises(inbound_rules.RuleError, match="хотя бы одно из from, subject, text"):
        inbound_rules.normalize({"title": "t", "accounts": [1], "wake": ["м"]})
    with pytest.raises(inbound_rules.RuleError, match="хотя бы одно из wake, attention"):
        inbound_rules.normalize({"title": "t", "text": ["заказ"]})
    with pytest.raises(inbound_rules.RuleError, match="from 1: не домен"):
        inbound_rules.normalize({"title": "t", "from": ["не домен"], "wake": ["м"]})
    with pytest.raises(inbound_rules.RuleError, match="kinds 1"):
        inbound_rules.normalize({"title": "t", "text": ["x1"], "kinds": ["issue"], "wake": ["м"]})
    with pytest.raises(inbound_rules.RuleError, match="больше 20"):
        inbound_rules.normalize({"title": "t", "from": [f"d{i}.ru" for i in range(21)], "wake": ["м"]})
    with pytest.raises(inbound_rules.RuleError, match="title: длиннее 80"):
        inbound_rules.normalize({"title": "т" * 81, "text": ["заказ"], "wake": ["м"]})
    with pytest.raises(inbound_rules.RuleError, match="wake 1"):
        inbound_rules.normalize({"title": "t", "text": ["заказ"], "wake": ["владелец"]})  # зарезервировано


# --- почта: тема в событии --------------------


def test_mail_event_subject_normalized_and_limited():
    raw = "Subject: Re: Fwd:  Новое сообщение   по заказу «Сайт»\r\nFrom: a@b.ru\r\n\r\nтело".encode()
    assert mail_convert.event_subject(raw) == "Новое сообщение по заказу «Сайт»"
    assert mail_convert.event_subject(b"From: a@b.ru\r\n\r\n") is None
    long_raw = ("Subject: " + "я" * 400 + "\r\n\r\n").encode()
    assert len(mail_convert.event_subject(long_raw)) == 300
    base = {
        "type": "upsert",
        "channel": {"ext_id": "t", "kind": "mail_thread"},
        "ext_id": "m",
        "ts": 1,
        "is_me": False,
        "kind": "text",
        "mentions_me": False,
    }
    assert parse_event({**base, "subject": "тема"}).subject == "тема"
    with pytest.raises(ValueError):
        parse_event({**base, "subject": "я" * 301})


# --- ингест --------------------


def _mail(ext, ts, *, chat="t1", subject=PHRASE + " «Сайт»", sender="no_reply@fl.example.com", **extra):
    event = {
        "type": "upsert",
        "channel": {"ext_id": chat, "kind": "mail_thread", "own_id_space": True},
        "ext_id": ext,
        "ts": ts,
        "sender": {"ext_id": sender, "name": "Площадка", "handle": sender},
        "is_me": False,
        "kind": "text",
        "text": "письмо",
        "mentions_me": False,
        "subject": subject,
        **extra,
    }
    return event


def _apply(db, account_id, events, now_ms=10_000):
    result = db.write_sync(
        lambda conn: ingest.apply_batch(
            conn, account_id=account_id, events=events, cursor=None, now_ms=now_ms
        )
    )
    assert result.dead == 0, result


def _add_rule(db, project_id, fields, *, now_ms=1):
    actor = {"kind": "ui", "token_id": None, "request_id": "t"}
    return db.write_sync(
        lambda conn: inbound_rules.add(conn, project_id, fields, actor=actor, now_ms=now_ms)
    ).id


def _rows(db):
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT ext_id, rule_hits, rule_attention FROM main.message ORDER BY id"
        ).fetchall()
    return {ext: (json.loads(hits) if hits else [], att) for ext, hits, att in rows}


@pytest.fixture
def mailbox(db):
    pid = insert_project(db, name="Hooli")
    other = insert_project(db, name="Чужой")
    acc = insert_account(db, source="mail")
    ch = insert_channel(db, acc, project_id=pid, ext_id="t1", kind="mail_thread", title="Тема треда")
    insert_channel(db, acc, project_id=other, ext_id="t2", kind="mail_thread", title="Чужое")
    insert_channel(db, acc, project_id=pid, ext_id="jira:x:K-1", kind="issue", title="K-1")
    return {"pid": pid, "other": other, "acc": acc, "ch": ch}


def test_ingest_writes_hits_attention_and_counter(db, mailbox):
    _apply(db, mailbox["acc"], [_mail("m0", 500)])  # до правила
    rid = _add_rule(
        db,
        mailbox["pid"],
        {
            "title": "заказы",
            "from": ["example.com"],
            "subject": [PHRASE],
            "wake": ["менеджер"],
            "attention": "react",
        },
    )
    _apply(
        db,
        mailbox["acc"],
        [
            _mail("m1", 1000),
            _mail("m2", 2000, subject="Счёт за услуги"),  # тема не та
            _mail("m3", 3000, chat="t2"),  # чужой проект
            {**_mail("m4", 4000, chat="jira:x:K-1"), "channel": {"ext_id": "jira:x:K-1", "kind": "issue"}},
            _mail("m5", 5000, is_me=True, sender="me@x.ru"),  # своё
        ],
        now_ms=20_000,
    )
    rows = _rows(db)
    assert rows["m0"] == ([], None)  # правило позже — старое не трогает
    assert rows["m1"] == ([rid], "react")
    assert rows["m2"] == ([], None)
    assert rows["m3"] == ([], None)
    assert rows["m4"] == ([], None)  # беседа трекера
    assert rows["m5"] == ([], None)
    with db.reader() as conn:
        rec = inbound_rules.get(conn, rid)
    assert (rec.hits, rec.last_hit_ts) == (1, 20_000)
    # правка правила не пересчитывает: тема/текст сменились — пометка как была
    _apply(db, mailbox["acc"], [{**_mail("m2", 2000), "type": "edit", "text": "новое", "edited_ts": 9000}])
    assert _rows(db)["m2"] == ([], None)
    # выключенное правило не срабатывает
    db.write_sync(
        lambda conn: inbound_rules.update(
            conn, mailbox["pid"], rid, {"enabled": False}, actor={"kind": "ui", "request_id": "t"}, now_ms=2
        )
    )
    _apply(db, mailbox["acc"], [_mail("m6", 6000)])
    assert _rows(db)["m6"] == ([], None)


def test_ingest_subject_falls_back_to_channel_title(db, mailbox):
    """Коннектор старше Ф63-1 темы не шлёт — сверка с названием беседы."""
    _add_rule(db, mailbox["pid"], {"title": "т", "subject": ["тема треда"], "attention": "info"})
    event = _mail("m1", 1000)
    del event["subject"]
    _apply(db, mailbox["acc"], [event])
    assert _rows(db)["m1"][1] == "info"


# --- внимание (П5) -----------------------------------------------------------------------------------------


def test_message_attention_rule_rules():
    ma = attention.message_attention
    # рассылка в «реагирую»-почте гаснет, правило react её поднимает (и сообщение бота)
    assert ma("react", 0, "mail_thread", 0, 1, None, None) == "info"
    assert ma("react", 0, "mail_thread", 0, 1, None, "react") == "react"
    assert ma("info", 0, "group", 1, 0, None, "react") == "react"
    # ручной режим и заглушка сильнее правила
    assert ma("info", 0, "mail_thread", 0, 1, "info", "react") == "info"
    assert ma("react", 0, "group", 0, 0, "react", "info") == "react"
    assert ma("muted", 0, "group", 0, 0, None, "react") == "muted"
    # info гасит, но не лички и не прямое упоминание в чате; письмо «мне в Кому» — гасит
    assert ma("react", 0, "mail_thread", 0, 0, None, "info") == "info"
    assert ma("react", 1, "mail_thread", 0, 0, None, "info") == "info"
    assert ma("react", 0, "dm", 0, 0, None, "info") == "react"
    assert ma("react", 0, "mpim", 0, 0, None, "info") == "react"
    assert ma("info", 1, "group", 0, 0, None, "info") == "react"
    assert ma("info", 0, "group", 0, 0, None, "info") == "info"


def _messages(db, channel_id):
    with db.reader() as conn:
        page = queries.list_messages(conn, UI, channel_id, before=None, limit=200)
    return {m["ext_id"]: m for m in page.messages}


def test_home_react_row_carries_rule_hits_and_titles(db, mailbox):
    """15.10: строка «Нужна реакция» несёт `rule_hits` последнего сообщения и `rules` (номер -> название)."""
    rid = _add_rule(db, mailbox["pid"], {"title": "Заказы", "subject": ["заказ"], "attention": "react"})
    _apply(db, mailbox["acc"], [_mail("m1", 1000, bulk=True)])
    with db.reader() as conn:
        rows = [r for r in queries.home_react(conn, UI) if r["channel"]["id"] == mailbox["ch"]]
    assert len(rows) == 1
    assert rows[0]["rule_hits"] == [rid]
    assert rows[0]["rules"] == [{"id": rid, "title": "Заказы"}]


def test_bulk_react_rule_makes_card_and_manual_wins(db, mailbox):
    rid = _add_rule(db, mailbox["pid"], {"title": "т", "subject": ["заказ"], "attention": "react"})
    _apply(
        db, mailbox["acc"], [_mail("m1", 1000, bulk=True), _mail("m2", 2000, bulk=True, subject="рассылка")]
    )
    msgs = _messages(db, mailbox["ch"])
    assert (msgs["m1"]["bulk"], msgs["m1"]["attention"]) == (True, "react")
    assert (msgs["m1"]["rule_hits"], msgs["m1"]["rule_attention"]) == ([rid], "react")
    assert (msgs["m2"]["attention"], msgs["m2"]["rule_hits"]) == ("info", [])
    with db.reader() as conn:
        channel = queries.get_channel(conn, UI, mailbox["ch"])
        home = {row["channel"]["id"] for row in queries.home_react(conn, UI)}
    assert channel["unread_react"] == 1 and mailbox["ch"] in home  # «Нужна реакция» — разборщик видит react
    # ручной «к сведению» владельца сильнее правила
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.channel SET attention = 'info' WHERE id = ?", (mailbox["ch"],))
    )
    assert _messages(db, mailbox["ch"])["m1"]["attention"] == "info"
    # заглушка — тоже
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET attention = NULL, muted = 1 WHERE id = ?", (mailbox["ch"],)
        )
    )
    assert _messages(db, mailbox["ch"])["m1"]["attention"] == "muted"


def test_info_rule_keeps_dm_and_direct_mention(db):
    pid = insert_project(db, name="P")
    acc = insert_account(db, source="slack")
    dm = insert_channel(db, acc, project_id=pid, ext_id="D1", kind="dm")
    grp = insert_channel(db, acc, project_id=pid, ext_id="C1", kind="group")
    _add_rule(db, pid, {"title": "т", "text": ["отчёт"], "attention": "info"})

    def ev(chat, kind, ext, ts, mention=False):
        return {
            "type": "upsert",
            "channel": {"ext_id": chat, "kind": kind},
            "ext_id": ext,
            "ts": ts,
            "sender": {"ext_id": "U2", "name": "Пётр"},
            "is_me": False,
            "kind": "text",
            "text": "отчёт готов",
            "mentions_me": mention,
        }

    _apply(
        db,
        acc,
        [
            ev("D1", "dm", "1.1", 1000),
            ev("C1", "group", "2.1", 2000, mention=True),
            ev("C1", "group", "2.2", 3000),
        ],
    )
    assert _messages(db, dm)["1.1"]["attention"] == "react"
    group = _messages(db, grp)
    assert group["2.1"]["attention"] == "react"  # прямое упоминание правило «к сведению» не гасит
    assert group["2.2"]["attention"] == "info"
    assert group["2.2"]["rule_attention"] == "info"


# --- wake (П4) ---------------------------------------------------------------------------------------------


def _ev(chat, ext, ts, text, *, bot=False):
    sender = {"ext_id": "U2", "name": "Пётр"}
    if bot:
        sender["is_bot"] = True
    return {
        "type": "upsert",
        "channel": {"ext_id": chat, "kind": "group"},
        "ext_id": ext,
        "ts": ts,
        "sender": sender,
        "is_me": False,
        "kind": "text",
        "text": text,
        "mentions_me": False,
    }


def _signals(db):
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT project_sub_id, chan_sub_id, channel_id, status, reason, cat, ready_ts, rules_json "
            "FROM main.session_chan_signal ORDER BY id"
        ).fetchall()
    return [(*r[:7], json.loads(r[7]) if r[7] else None) for r in rows]


async def test_wake_bypasses_batch_personal_and_channel_mute(client, db, world, procs, clock):
    w = world
    manager = (await _sub(client, w, A, mode="personal", role="менеджер", keywords=["штраф"]))["sub"]["id"]
    other = (await _sub(client, w, B, mode="personal", role="маркетолог"))["sub"]["id"]
    rid = _add_rule(db, w["pid"], {"title": "заказы", "text": ["заказ"], "wake": ["Менеджер"]})
    now = _now(clock)
    _apply(
        db,
        w["acc"],
        [_ev("C1", "1.1", now, "новый заказ"), _ev("MUTED", "1.2", now, "заказ в заглушённой", bot=True)],
        now_ms=now,
    )
    rows = _signals(db)
    woken = [r for r in rows if r[0] == manager]
    assert [(r[2], r[3], r[5], r[6], r[7]) for r in woken] == [
        (w["ch"]["C1"], "pending", "keyword", None, [rid]),  # без срока пачки — сразу
        (w["ch"]["MUTED"], "pending", "keyword", None, [rid]),  # мимо заглушки беседы и отсева бота
    ]
    # чужая роль — как без правила: «лично мне» отсеивает
    assert [(r[3], r[4]) for r in rows if r[0] == other] == [("skipped", "not_personal")]
    signals = await _wait(client, w, manager, A)
    assert {s["channel_id"]: s.get("rules") for s in signals} == {
        w["ch"]["C1"]: [rid],
        w["ch"]["MUTED"]: [rid],
    }
    # вывод ожидания: только номер правила
    body = {"sub_id": manager, "project_id": w["pid"], "signals": signals}
    parsed = sub_wait.valid_signals(body)
    assert parsed is not None and all(s["rules"] == [rid] for s in parsed[2])
    text = sub_wait.signals_text(manager, w["pid"], parsed[2], "hub sub wait 1")
    assert f"правило #{rid}" in text and "заказы" not in text


async def test_wake_respects_accounts_project_mute_and_cards(client, db, world, procs, clock):
    w = world
    filtered = (await _sub(client, w, A, mode="messages", role="менеджер", accounts_except=[w["acc"]]))[
        "sub"
    ]["id"]
    cards = (await _sub(client, w, B, role="разборщик"))["sub"]["id"]
    _add_rule(db, w["pid"], {"title": "т", "text": ["заказ"], "wake": ["менеджер", "разборщик"]})
    now = _now(clock)
    _apply(db, w["acc"], [_ev("C1", "1.1", now, "заказ")], now_ms=now)
    assert _signals(db) == []  # фильтр аккаунтов соблюдён; «карточки» сигнала сообщений не получают
    assert filtered and cards
    # заглушённый проект — правило не будит
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.session_sub SET accounts_json = NULL WHERE id = ?", (filtered,)
        )
    )
    db.write_sync(lambda conn: conn.execute("UPDATE main.project SET muted = 1 WHERE id = ?", (w["pid"],)))
    _apply(db, w["acc"], [_ev("C1", "1.2", now + 1, "заказ")], now_ms=now + 1)
    assert _signals(db) == []
    db.write_sync(lambda conn: conn.execute("UPDATE main.project SET muted = 0 WHERE id = ?", (w["pid"],)))
    _apply(db, w["acc"], [_ev("C1", "1.3", now + 2, "заказ")], now_ms=now + 2)
    assert [r[0] for r in _signals(db)] == [filtered]


async def test_wake_list_shows_listening(client, db, world, procs):
    w = world
    await _sub(client, w, A, mode="messages", role="менеджер")
    await _sub(client, w, B, role="разборщик")
    rid = _add_rule(
        db, w["pid"], {"title": "т", "text": ["заказ"], "wake": ["Менеджер", "разборщик", "никто"]}
    )
    with db.reader() as conn:
        [rule] = inbound_rules.list_rules(conn, w["pid"])
    assert rule["id"] == rid
    assert rule["wake_status"] == [
        {"role": "Менеджер", "listening": "messages"},
        {"role": "разборщик", "listening": "cards"},
        {"role": "никто", "listening": "none"},
    ]
    assert "будит «Менеджер», «разборщик», «никто»" in rule["description"]


# --- ручки --------------------


def _audit(db, prefix="inbound_rule_"):
    with db.reader() as conn:
        return [
            (action, json.loads(detail))
            for action, detail in conn.execute(
                "SELECT action, detail_json FROM main.audit WHERE action LIKE ? ORDER BY id", (prefix + "%",)
            )
        ]


async def test_routes_ui_crud_and_read_marks(client, db, clock, mailbox):
    cookie, _csrf = await login_via_code(client, db, clock)
    h = csrf_headers(cookie)
    base = f"/api/v1/projects/{mailbox['pid']}/inbound-rules"
    r = await client.post(
        base,
        json={"title": "Заказы", "from": ["example.com"], "subject": [PHRASE], "attention": "react"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    rule = r.json()
    assert rule["created_by_role"] is None and rule["enabled"] is True
    assert rule["match"]["from"] == ["example.com"]
    assert rule["action"] == {"wake": [], "attention": "react", "archive": False}
    r = await client.post(
        base,
        json={"title": "плохо", "text": ["раз два три четыре пять шесть семь"], "attention": "info"},
        headers=h,
    )
    assert r.status_code == 422 and "больше 6 слов" in r.json()["detail"]
    # приём и пометка в ленте
    _apply(db, mailbox["acc"], [_mail("m1", 1000)])
    r = await client.get(f"/api/v1/channels/{mailbox['ch']}/messages")
    [msg] = r.json()["messages"]
    assert msg["rule_hits"] == [rule["id"]] and msg["rule_attention"] == "react"
    assert msg["rules"] == [{"id": rule["id"], "title": "Заказы"}]
    r = await client.get(f"/api/v1/messages/{msg['id']}")
    assert r.json()["rules"] == [{"id": rule["id"], "title": "Заказы"}]
    # правка: выключить, снять внимание нельзя без действия
    r = await client.patch(f"{base}/{rule['id']}", json={"enabled": False}, headers=h)
    assert r.status_code == 200 and r.json()["enabled"] is False
    r = await client.patch(f"{base}/{rule['id']}", json={"attention": None}, headers=h)
    assert r.status_code == 422
    r = await client.patch(f"{base}/{rule['id']}", json={"attention": None, "wake": ["менеджер"]}, headers=h)
    assert r.status_code == 200
    assert r.json()["action"] == {"wake": ["менеджер"], "attention": None, "archive": False}
    # чужой проект в пути — 404
    r = await client.patch(
        f"/api/v1/projects/{mailbox['other']}/inbound-rules/{rule['id']}", json={"enabled": True}, headers=h
    )
    assert r.status_code == 404
    r = await client.get(base)
    assert [x["id"] for x in r.json()["rules"]] == [rule["id"]]
    r = await client.delete(f"{base}/{rule['id']}", headers=h)
    assert r.status_code == 200 and r.json()["removed"] == rule["id"]
    assert [a for a, _ in _audit(db)] == [
        "inbound_rule_add",
        "inbound_rule_update",
        "inbound_rule_update",
        "inbound_rule_remove",
    ]
    assert _audit(db)[0][1]["by"] == "ui"
    # удалённое правило: пометка на сообщении остаётся, названия нет
    r = await client.get(f"/api/v1/channels/{mailbox['ch']}/messages")
    assert r.json()["messages"][0]["rules"] == [{"id": rule["id"], "title": None}]


async def test_routes_session_own_project_only(client, db, world, procs):
    w = world
    other_pid = insert_project(db, name="Другой")
    db.write_sync(lambda conn: subs.set_project_subs(conn, other_pid, True, now_ms=0))
    await _sub(client, w, A, mode="messages", role="менеджер")
    await _sub(client, w, B)  # без роли
    me = _session(w, A, full=False)
    base = f"/api/v1/projects/{w['pid']}/inbound-rules"
    body = {"title": "т", "text": ["заказ"], "wake": ["менеджер"], "session": me}
    r = await client.post(base, json=body, headers=_h(w["tok"]))
    assert r.status_code == 201, r.text
    rule = r.json()
    assert rule["created_by_role"] == "менеджер" and isinstance(rule["created_by_session"], int)
    assert _audit(db)[0][1]["by"] == "менеджер"
    # чужой проект — 403
    r = await client.post(f"/api/v1/projects/{other_pid}/inbound-rules", json=body, headers=_h(w["tok"]))
    assert r.status_code == 403 and "только своего проекта" in r.json()["detail"]
    # без роли — 403; без сессии — 422; сессии нет в живых — 409
    r = await client.post(base, json={**body, "session": _session(w, B, full=False)}, headers=_h(w["tok"]))
    assert r.status_code == 403 and "с ролью" in r.json()["detail"]
    r = await client.post(base, json={k: v for k, v in body.items() if k != "session"}, headers=_h(w["tok"]))
    assert r.status_code == 422
    r = await client.post(base, json={**body, "session": _session(w, C, full=False)}, headers=_h(w["tok"]))
    assert r.status_code == 409
    # правка и удаление — своей сессией
    r = await client.patch(
        f"{base}/{rule['id']}", json={"enabled": False, "session": me}, headers=_h(w["tok"])
    )
    assert r.status_code == 200 and r.json()["enabled"] is False
    r = await client.request("DELETE", f"{base}/{rule['id']}", json={"session": me}, headers=_h(w["tok"]))
    assert r.status_code == 200
    # чтение — bearer своего проекта; токен без проекта — 404
    r = await client.get(base, headers=_h(w["tok"]))
    assert r.status_code == 200 and r.json()["rules"] == []
    narrow = db.write_sync(
        lambda conn: security.create_api_token(
            conn,
            name="mcp:narrow",
            kind="interactive",
            scopes=["read", "draft"],
            project_ids=[other_pid],
            expires_ts=None,
        )
    ).token
    r = await client.get(base, headers=_h(narrow))
    assert r.status_code == 404


async def test_routes_limit_50(client, db, clock, mailbox):
    cookie, _csrf = await login_via_code(client, db, clock)
    h = csrf_headers(cookie)
    for n in range(inbound_rules.RULES_MAX):
        _add_rule(db, mailbox["pid"], {"title": f"т{n}", "text": [f"слово{n}"], "attention": "info"})
    r = await client.post(
        f"/api/v1/projects/{mailbox['pid']}/inbound-rules",
        json={"title": "лишнее", "text": ["заказ"], "attention": "info"},
        headers=h,
    )
    assert r.status_code == 429


# --- вывод MCP ------------------------------------------------------------------------------------------


def test_mcp_message_lines_rule_mark_inside_data():
    message = {
        "id": 5,
        "ext_id": "m1",
        "ts": 0,
        "text": "письмо",
        "rules": [{"id": 3, "title": "Заказы\nплощадки"}],
    }
    [line] = fmt.message_lines(message)
    assert "(правило #3: Заказы площадки)" in line
    message["rules"] = [{"id": 4, "title": None}, {"id": True}, "мусор"]
    [line] = fmt.message_lines(message)
    assert "(правило #4)" in line and "#True" not in line


def test_chan_subs_rules_json_merge_and_cap():
    assert chan_subs.load_rules(chan_subs._rules_json(None, [3, 1])) == [3, 1]  # noqa: SLF001
    merged = chan_subs._rules_json(json.dumps([1, 2, 3]), [3, 4, 5, 6])  # noqa: SLF001
    assert json.loads(merged) == [1, 2, 3, 4, 5]
    assert chan_subs.load_rules('[1, "x", true, 0]') == [1]


# --- владелец 22.10: правило «в архив» --------------------------------------------------------------------------


def _archived(db, channel_id):
    with db.reader() as conn:
        return conn.execute("SELECT archived FROM main.channel WHERE id = ?", (channel_id,)).fetchone()[0]


def test_archive_rule_archives_channel_and_skips_signals(db, mailbox):
    rid = _add_rule(db, mailbox["pid"], {"title": "тест-заявки", "subject": [PHRASE], "archive": True})
    with db.reader() as conn:
        assert inbound_rules.rule_dict(conn, inbound_rules.get(conn, rid))["action"]["archive"] is True
        assert "сразу в архив" in inbound_rules.rule_dict(conn, inbound_rules.get(conn, rid))["description"]
    _apply(db, mailbox["acc"], [_mail("m1", 1000, subject="Счёт за услуги")])
    assert _archived(db, mailbox["ch"]) == 0  # тема не та
    _apply(db, mailbox["acc"], [_mail("m2", 2000)], now_ms=20_000)
    assert _archived(db, mailbox["ch"]) == 1
    assert _rows(db)["m2"] == ([rid], None)
    # новая беседа с тем же условием — сразу в архив
    _apply(db, mailbox["acc"], [_mail("m3", 3000, chat="t9")], now_ms=30_000)
    with db.reader() as conn:
        archived, project_id = conn.execute(
            "SELECT archived, project_id FROM main.channel WHERE ext_id = 't9'"
        ).fetchone()
    # без правила раскладки беседа «Неразобранного» — правило проекта не действует
    assert (archived, project_id) == (0, None)


def test_archive_rule_only_from_owner():
    base = {"title": "т", "subject": ["заказ"]}
    form = inbound_rules.normalize({**base, "archive": True})
    assert form["action"] == {"archive": True}
    assert inbound_rules.normalize({**base, "archive": None, "wake": ["р"]})["action"] == {"wake": ["р"]}
    with pytest.raises(inbound_rules.RuleError) as exc:
        inbound_rules.normalize({**base, "archive": "да"})
    assert exc.value.status == 422
    session = {"kind": "interactive", "role": "менеджер"}
    for actor in ({"kind": "ui"}, {"kind": "admin"}):
        inbound_rules._check_archive(actor, form)  # noqa: SLF001
    with pytest.raises(inbound_rules.RuleError) as exc:
        inbound_rules._check_archive(session, form)  # noqa: SLF001
    assert exc.value.status == 403
    inbound_rules._check_archive(session, inbound_rules.normalize({**base, "wake": ["р"]}))  # noqa: SLF001


async def test_archive_rule_session_cannot_add_edit_or_remove(client, db, world, procs):
    w = world
    await _sub(client, w, A, mode="messages", role="менеджер")
    me = _session(w, A, full=False)
    base = f"/api/v1/projects/{w['pid']}/inbound-rules"
    r = await client.post(
        base, json={"title": "т", "text": ["заказ"], "archive": True, "session": me}, headers=_h(w["tok"])
    )
    assert r.status_code == 403 and "только владелец" in r.json()["detail"]
    rid = _add_rule(db, w["pid"], {"title": "архив", "text": ["заказ"], "archive": True})
    r = await client.patch(f"{base}/{rid}", json={"text": ["все"], "session": me}, headers=_h(w["tok"]))
    assert r.status_code == 403
    r = await client.request("DELETE", f"{base}/{rid}", json={"session": me}, headers=_h(w["tok"]))
    assert r.status_code == 403
