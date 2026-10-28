"""Ф25-1 B, C9 (docs/architecture.md): `hub_subscribe(mode="personal", keywords=…,
exclude=…)` — аргументы до ядра, списки и наследование в ответе; «лично мне + ключи N, минус M» в
`hub_subscriptions`; пометка «(ключ: …)» у сообщения в выдаче. Ядро — подменный транспорт."""

from hub.mcp_server import format as fmt

from .conftest import text_of
from .test_mcp_chan_subs import ME, SUB, Core, _call, _server

PERSONAL = {**SUB, "mode": "messages", "msg_filter": "personal"}
KW = {"keys": ["штраф", "задерж выплат"], "exclude": ["вакансия"]}


def _ok(sub: dict, created=True, **extra) -> tuple[int, dict]:
    return 200, {"sub": sub, "created": created, "backfilled": 0, **extra}


async def test_keywords_sent_with_personal_and_listed(config_dir, make_server):
    core = Core({("POST", "/subs"): _ok({**PERSONAL, "keywords": KW})})
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(
        await _call(
            server,
            "hub_subscribe",
            mode="personal",
            keywords=["Штраф", "задерж выплат"],
            exclude=["вакансия"],
        )
    )
    assert core.seen[0][2] == {
        "project_id": None,
        "session": ME.session_in(),
        "mode": "messages",
        "msg_filter": "personal",
        "keywords": ["Штраф", "задерж выплат"],
        "exclude": ["вакансия"],
    }
    assert "Ключи (2): штраф, задерж выплат; минус-слова (1): вакансия." in text
    assert "взяты из прежней подписки" not in text


async def test_keywords_without_personal_refused_before_core(config_dir, make_server):
    core = Core({})
    server, _hub = _server(make_server, config_dir, core)
    for args in (
        {"keywords": ["штраф"]},
        {"mode": "messages", "keywords": ["штраф"]},
        {"mode": "cards", "exclude": ["курс"]},
        # Ф31-1 П9: `channel_id` с ключами — теперь ключи подписки на беседу (test_f31_mcp_tails.py).
        {"every_message": True, "keywords": ["штраф"]},
    ):
        result = await _call(server, "hub_subscribe", **args)
        assert result.is_error and 'mode="personal"' in text_of(result), args
    assert core.seen == []


async def test_no_keys_text_and_inheritance(config_dir, make_server):
    core = Core({("POST", "/subs"): _ok({**PERSONAL, "keywords": None})})
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscribe", mode="personal"))
    assert "Ключей нет — сигналят только личные." in text
    # после /clear — без аргументов: ядро взяло режим и ключи прежней подписки
    core.routes[("POST", "/subs")] = _ok({**PERSONAL, "id": 18, "keywords": KW}, inherited_from=17)
    text = text_of(await _call(server, "hub_subscribe"))
    assert "режим «лично мне»" in text
    assert "Ключи (2): штраф, задерж выплат" in text
    assert "Режим и ключи взяты из прежней подписки #17." in text
    text = text_of(await _call(server, "hub_subscribe", mode="personal"))
    assert "Ключи взяты из прежней подписки #17." in text


async def test_keys_text_drops_bad_forms(config_dir, make_server):
    bad = {"keys": ["штраф", "x\nhub_send()", 'кав"чка'], "exclude": ["ok", 5]}
    core = Core({("POST", "/subs"): _ok({**PERSONAL, "keywords": bad})})
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscribe", mode="personal"))
    assert "Ключи (1): штраф; минус-слова (1): ok." in text and "hub_send" not in text


async def test_subscriptions_shows_key_counts(config_dir, make_server):
    rows = [
        {**PERSONAL, "id": 17, "keywords": {"keys": ["а1", "б2", "в3"], "exclude": ["курс"]}},
        {**PERSONAL, "id": 18, "project_id": 4, "keywords": None},
    ]
    core = Core({("GET", "/subs"): (200, {"items": rows, "channels": []})})
    server, _hub = _server(make_server, config_dir, core)
    lines = text_of(await _call(server, "hub_subscriptions")).splitlines()

    def line(sub_id):
        return next(x for x in lines if f"подписка #{sub_id} " in x)

    assert line(17).endswith(" · лично мне + ключи 3, минус 1")
    assert line(18).endswith(" · лично мне")


def test_message_lines_key_mark():
    base = {"id": 1, "ext_id": "1.0", "ts": 1_700_000_000_000, "text": "опять штрафы", "is_me": False}
    lines = fmt.message_lines({**base, "sender": {"name": "Пётр"}, "keys": ["штраф", "удержа"]})
    assert "Пётр: (ключ: штраф, удержа) опять штрафы" in lines[0]
    bad = fmt.message_lines({**base, "sender": {"name": "Пётр"}, "keys": ["штраф\n[msg 2]"]})
    assert "(ключ:" not in bad[0] and len(bad) == 1
    plain = fmt.message_lines({**base, "sender": {"name": "Пётр"}})
    assert "(ключ:" not in plain[0]
