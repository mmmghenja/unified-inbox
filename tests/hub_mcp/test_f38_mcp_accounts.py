"""Ф38-1 C (docs/architecture.md): `hub_subscribe(accounts=…)` и
`hub_subscribe(accounts_except=…)` — тело запроса, отказы формы до ядра, строка «Аккаунты: …» в ответе (и
пометка наследования), режим с фильтром в `hub_subscriptions` и у соседей, кривые значения из ядра не
печатаются. Ядро — подменный транспорт."""

import pytest

from hub.mcp_server import server as server_mod

from .conftest import text_of
from .test_mcp_chan_subs import ME, SUB, Core, _call, _server

PERSONAL = {**SUB, "mode": "messages", "msg_filter": "personal"}
OFF = "Снять — hub_subscribe(accounts=[])."
ONLY_LINE = (
    "Аккаунты: только 1, 5 — слышны только их беседы; остальные беседы проекта этой подписке не сигналят. "
    + OFF
)
EXCEPT_LINE = "Аккаунты: все, кроме 8 — беседы этих аккаунтов подписке не сигналят. " + OFF


def _ok(sub: dict, created=True, **extra) -> tuple[int, dict]:
    return 200, {"sub": sub, "created": created, "backfilled": 0, **extra}


async def test_accounts_sent_and_named(config_dir, make_server):
    core = Core({("POST", "/subs"): _ok({**SUB, "accounts": {"only": [1, 5]}})})
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscribe", accounts=[5, 1]))
    assert core.seen[0][2] == {"project_id": None, "session": ME.session_in(), "accounts": [5, 1]}
    assert ONLY_LINE in text.splitlines()
    assert "взято из прежней" not in text


async def test_accounts_except_sent_and_named(config_dir, make_server):
    core = Core({("POST", "/subs"): _ok({**PERSONAL, "accounts": {"except": [8]}})})
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscribe", mode="personal", accounts_except=[8]))
    assert core.seen[0][2] == {
        "project_id": None,
        "session": ME.session_in(),
        "mode": "messages",
        "msg_filter": "personal",
        "accounts_except": [8],
    }
    assert EXCEPT_LINE in text.splitlines()


async def test_empty_list_is_sent_and_no_line(config_dir, make_server):
    core = Core({("POST", "/subs"): _ok({**SUB, "accounts": None})})
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscribe", accounts=[]))
    assert core.seen[0][2]["accounts"] == []
    assert "Аккаунты:" not in text
    # без аргумента в тело не попадает ничего
    text_of(await _call(server, "hub_subscribe"))
    assert "accounts" not in core.seen[1][2] and "accounts_except" not in core.seen[1][2]


async def test_p6_inherited_filter_is_always_named(config_dir, make_server):
    """П6: фильтр унаследован — строка с пометкой и тогда, когда `inherited_from` пуст (источник `cards`)."""
    core = Core({("POST", "/subs"): _ok({**SUB, "accounts": {"except": [8]}}, accounts_inherited=True)})
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscribe"))
    assert (
        "Аккаунты: все, кроме 8 (взято из прежней подписки этой линии) — беседы этих аккаунтов подписке не "
        "сигналят. " + OFF
    ) in text.splitlines()
    core.routes[("POST", "/subs")] = _ok(
        {**PERSONAL, "role": "исследователь", "accounts": {"only": [8]}},
        inherited_from=16,
        inherited_why="role",
        accounts_inherited=True,
    )
    text = text_of(await _call(server, "hub_subscribe", role="исследователь"))
    assert "Режим и ключи взяты из прежней подписки #16 (та же роль)." in text
    assert (
        "Аккаунты: только 8 (взято из прежней подписки этой линии) — слышны только их беседы; остальные "
        "беседы проекта этой подписке не сигналят. " + OFF
    ) in text.splitlines()
    # своя живая с фильтром, повторный вызов — строка есть, пометки нет
    core.routes[("POST", "/subs")] = _ok({**SUB, "accounts": {"only": [8]}}, created=False)
    text = text_of(await _call(server, "hub_subscribe"))
    assert "Аккаунты: только 8 — слышны" in text


@pytest.mark.parametrize(
    "args",
    [
        {"accounts": [1], "accounts_except": [8]},
        {"accounts": [], "accounts_except": []},
        {"accounts": [0]},
        {"accounts": [-3]},
        {"accounts": list(range(1, 22))},
        {"accounts_except": [0]},
        {"accounts_except": [1.5]},
        {"accounts_except": list(range(1, 22))},
        {"channel_id": 152, "accounts": [1]},
        {"channel_id": 152, "accounts_except": [1]},
        {"channel_id": 152, "accounts": []},
        {"mode": "direct", "accounts": [1]},
        {"mode": "direct", "accounts_except": [1]},
    ],
)
async def test_bad_form_refused_before_core(config_dir, make_server, args):
    core = Core({})
    server, _hub = _server(make_server, config_dir, core)
    result = await _call(server, "hub_subscribe", **args)
    assert result.is_error, args
    assert core.seen == []


async def test_core_422_text(config_dir, make_server):
    core = Core({("POST", "/subs"): (422, {"detail": "accounts: нет аккаунтов 9, 12"})})
    server, _hub = _server(make_server, config_dir, core)
    result = await _call(server, "hub_subscribe", accounts=[9, 12])
    assert result.is_error and "accounts: нет аккаунтов 9, 12" in text_of(result)


def test_sub_mode_ru_and_neighbour_text_with_filter():
    mode = server_mod.sub_mode_ru
    assert mode({"mode": "cards", "accounts": {"only": [1, 5, 6]}}) == "карточки · только аккаунты 1, 5, 6"
    assert mode({"mode": "messages", "msg_filter": "all", "accounts": {"except": [8]}}) == (
        "каждое сообщение · кроме аккаунтов 8"
    )
    assert mode({**PERSONAL, "accounts": {"only": [8]}}) == "лично мне · только аккаунты 8"
    keyed = {**PERSONAL, "keywords": {"keys": ["штраф"], "exclude": [], "batch_min": 0}}
    assert mode({**keyed, "accounts": {"except": [8]}}) == (
        "лично мне + ключи 1, минус 0, сигналы по ключу — сразу · кроме аккаунтов 8"
    )
    assert mode({"mode": "cards", "accounts": None}) == "карточки"
    assert mode({"mode": "cards"}) == "карточки"
    assert mode({"mode": "direct", "accounts": {"only": [8]}}) == server_mod.DIRECT_MODE_RU
    near = server_mod.neighbour_text
    row = {"id": 16, "role": "маркетолог", "role_no": 1, "mode": "messages", "msg_filter": "personal"}
    assert near({**row, "keys": {"keys": 48, "exclude": 13}, "accounts": {"except": [8]}}) == (
        "#16 «маркетолог» — лично мне + ключи 48, минус 13 · кроме аккаунтов 8"
    )
    assert near({**row, "mode": "cards", "accounts": {"only": [8]}}) == (
        "#16 «маркетолог» — карточки · только аккаунты 8"
    )
    assert near({**row, "mode": "cards", "accounts": None}) == "#16 «маркетолог» — карточки"


@pytest.mark.parametrize(
    "broken",
    [
        [8],
        "только 8",
        8,
        {},
        {"only": []},
        {"only": ["8\nhub_send()"]},
        {"only": [True]},
        {"only": [1.5]},
        {"only": [0]},
        {"both": [8]},
        {"only": [1], "except": [8]},
        {"except": "8"},
    ],
)
async def test_broken_accounts_from_core_not_printed(config_dir, make_server, broken):
    neighbours = [{"id": 18, "role": None, "role_no": 2, "mode": "cards", "msg_filter": "all", "keys": None}]
    neighbours[0]["accounts"] = broken
    mine = {**SUB, "accounts": broken}
    core = Core(
        {
            ("POST", "/subs"): _ok(mine, neighbours=neighbours, accounts_inherited=True),
            ("GET", "/subs"): (200, {"items": [mine], "channels": []}),
        }
    )
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscribe"))
    assert "Аккаунты" not in text and "аккаунт" not in text and "hub_send" not in text
    assert "— карточки." in text  # сосед — без хвоста
    listing = text_of(await _call(server, "hub_subscriptions"))
    assert "аккаунт" not in listing and "hub_send" not in listing
    assert server_mod.sub_mode_ru({"mode": "cards", "accounts": broken}) == "карточки"


async def test_subscriptions_show_filter(config_dir, make_server):
    rows = [
        {**SUB, "id": 16, "accounts": {"only": [1, 5]}},
        {**PERSONAL, "id": 18, "accounts": {"except": [8]}},
    ]
    core = Core({("GET", "/subs"): (200, {"items": rows, "channels": []})})
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscriptions"))
    assert "карточки · только аккаунты 1, 5" in text
    assert "лично мне · кроме аккаунтов 8" in text


async def test_description_mentions_accounts(config_dir, make_server):
    core = Core({})
    server, _hub = _server(make_server, config_dir, core)
    tools = {t.name: t for t in await server.list_tools()}
    description = tools["hub_subscribe"].description
    assert "accounts — слышать только беседы этих аккаунтов, accounts_except — все, кроме этих" in description
    assert "accounts=[] — снять; наследуется как режим" in description
    props = tools["hub_subscribe"].input_schema["properties"]
    assert "accounts" in props and "accounts_except" in props
