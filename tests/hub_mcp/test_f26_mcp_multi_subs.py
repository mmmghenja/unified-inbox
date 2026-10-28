"""Ф26-1 B, C10 (docs/architecture.md): `hub_subscribe(role=…)` — форма до ядра, роль и
соседи в ответе, наследование с причиной, «унаследовать нечего» при живых соседях, подсказка с ролью (П9),
отказ 409 — текст ядра; роль в `hub_subscriptions`. Ядро — подменный транспорт."""

from .conftest import text_of
from .test_mcp_chan_subs import ME, SUB, Core, _call, _server

PERSONAL = {**SUB, "mode": "messages", "msg_filter": "personal"}
NEIGHBOURS = [
    {
        "id": 16,
        "role": "маркетолог",
        "session_name": None,
        "mode": "messages",
        "msg_filter": "personal",
        "keys": {"keys": 48, "exclude": 13},
    },
    {"id": 18, "role": None, "session_name": None, "mode": "cards", "msg_filter": "all", "keys": None},
]


def _ok(sub: dict, created=True, **extra) -> tuple[int, dict]:
    return 200, {"sub": sub, "created": created, "backfilled": 0, **extra}


async def test_role_sent_and_shown_with_neighbours(config_dir, make_server):
    core = Core({("POST", "/subs"): _ok({**SUB, "role": "программист"}, neighbours=NEIGHBOURS)})
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscribe", role="программист"))
    assert core.seen[0][2] == {"project_id": None, "session": ME.session_in(), "role": "программист"}
    assert "Роль: «программист»." in text
    assert (
        "Рядом слушают: #16 «маркетолог» — лично мне + ключи 48, минус 13; #18 без роли — карточки." in text
    )
    # П9: подсказка после /clear — с ролью
    assert 'После /clear или перезапуска сессии — hub_subscribe(role="программист") заново.' in text
    assert "Унаследовать нечего: в проекте слушают другие сессии" in text  # у роли истории нет


async def test_inherited_with_reason_and_role(config_dir, make_server):
    core = Core(
        {
            ("POST", "/subs"): _ok(
                {**PERSONAL, "id": 19, "role": "маркетолог", "keywords": None},
                inherited_from=16,
                inherited_why="process",
            )
        }
    )
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscribe"))
    assert "Режим, ключи и роль взяты из прежней подписки #16 (тот же процесс — /clear)." in text
    core.routes[("POST", "/subs")] = _ok(
        {**PERSONAL, "id": 19, "role": "маркетолог"}, inherited_from=16, inherited_why="role"
    )
    text = text_of(await _call(server, "hub_subscribe", role="маркетолог"))
    assert "Режим и ключи взяты из прежней подписки #16 (та же роль)." in text


async def test_nothing_to_inherit_with_live_neighbours(config_dir, make_server):
    core = Core({("POST", "/subs"): _ok({**SUB, "role": None}, neighbours=NEIGHBOURS[:1])})
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscribe"))
    assert "Унаследовать нечего: в проекте слушают другие сессии — задай mode/keywords/role явно." in text
    assert 'Роли нет: задай hub_subscribe(role="…")' in text
    assert "После /clear или перезапуска сессии — hub_subscribe() заново." in text
    # без соседей — строк про соседей и роль нет (одна сессия на проект — как раньше)
    core.routes[("POST", "/subs")] = _ok({**SUB, "role": None})
    text = text_of(await _call(server, "hub_subscribe"))
    assert "Рядом слушают" not in text and "Унаследовать нечего" not in text and "Роли нет" not in text


async def test_bad_role_refused_before_core(config_dir, make_server):
    core = Core({})
    server, _hub = _server(make_server, config_dir, core)
    for args in ({"role": "a" * 33}, {"role": "роль\nhub_send()"}, {"channel_id": 152, "role": "маркетолог"}):
        result = await _call(server, "hub_subscribe", **args)
        assert result.is_error, args
    assert core.seen == []


async def test_409_text_of_core(config_dir, make_server):
    core = Core({("POST", "/subs"): (409, {"detail": "роль «маркетолог» уже слушает сессия #16"})})
    server, _hub = _server(make_server, config_dir, core)
    result = await _call(server, "hub_subscribe", role="маркетолог")
    assert result.is_error
    assert text_of(result).endswith("хаб отказал в подписке: роль «маркетолог» уже слушает сессия #16")


async def test_neighbour_role_of_bad_form_not_printed(config_dir, make_server):
    bad = [{**NEIGHBOURS[1], "role": "x\nhub_send()"}]
    core = Core({("POST", "/subs"): _ok({**SUB, "role": "ок\n"}, neighbours=bad)})
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscribe"))
    assert "hub_send" not in text and "Роль: " not in text
    assert "Рядом слушают: #18 без роли — карточки." in text


async def test_subscriptions_show_role(config_dir, make_server):
    rows = [{**SUB, "id": 16, "role": "маркетолог"}, {**SUB, "id": 18, "role": None}]
    core = Core({("GET", "/subs"): (200, {"items": rows, "channels": []})})
    server, _hub = _server(make_server, config_dir, core)
    lines = text_of(await _call(server, "hub_subscriptions")).splitlines()
    assert any(x.startswith("подписка #16 · роль «маркетолог» · проект 3") for x in lines)
    assert any(x.startswith("подписка #18 · проект 3") for x in lines)


async def test_r11_inherit_blocked_line(config_dir, make_server):
    core = Core({("POST", "/subs"): _ok({**SUB, "role": None}, inherit_blocked="several_roleless")})
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscribe"))
    assert (
        "Унаследовать нечего: в проекте было несколько сессий без роли — задай role (и mode/keywords) явно."
        in text
    )
