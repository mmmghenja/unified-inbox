"""Ф31-1 (docs/architecture.md и B5 + П9, П12), MCP.

- B8 + П9: `hub_subscribe(channel_id=…, keywords=…, exclude=…)` передаёт ключи ядру (проверка «только с
  personal» — у подписки на проект, не у беседы); ответ и `hub_subscriptions` показывают ключи.
- B5 + П12: `hub_download` по `failed`, который сессия уже возвращала в очередь меньше 10 мин назад, — ответ
  «повтори после ЧЧ:ММ» (не ошибка), срок — из ответа ядра. Ядро настоящее (`ASGITransport`)."""

from hub.mcp_server import format as fmt

from .conftest import text_of
from .test_mcp_chan_subs import CHAN, ME, Core, _call, _server
from .test_mcp_download import _Clock, _Counting, _download, _World
from .test_mcp_download import _server as _download_server

KW = {"keys": ["штраф", "задерж выплат"], "exclude": ["вакансия"]}


async def test_b8_subscribe_channel_with_keywords(config_dir, make_server):
    core = Core(
        {("POST", "/chan-subs"): (200, {"sub": {**CHAN, "keywords": KW}, "created": True, "count": 1})}
    )
    server, _hub = _server(make_server, config_dir, core)
    result = await _call(
        server, "hub_subscribe", channel_id=152, keywords=["Штраф", "задерж выплат"], exclude=["вакансия"]
    )
    text = text_of(result)
    assert not result.is_error, text
    assert core.seen == [
        (
            "POST",
            "/chan-subs",
            {
                "channel_id": 152,
                "session": ME.session_in(),
                "keywords": ["Штраф", "задерж выплат"],
                "exclude": ["вакансия"],
            },
        )
    ]
    assert "Сигнал — на личное и на сообщения с ключом. Ключи (2): штраф, задерж выплат" in text
    assert "минус-слова (1): вакансия" in text
    assert "hub_subscribe(channel_id=…, keywords=[])" in text
    # снять ключи — пустой список доходит до ядра
    core.routes[("POST", "/chan-subs")] = (
        200,
        {"sub": {**CHAN, "keywords": None}, "created": False, "count": 1},
    )
    text = text_of(await _call(server, "hub_subscribe", channel_id=152, keywords=[]))
    assert core.seen[1][2]["keywords"] == [] and "exclude" not in core.seen[1][2]
    assert "Сигнал — на каждое новое входящее этой беседы." in text


async def test_b8_channel_keywords_refusals_before_core(config_dir, make_server):
    core = Core({})
    server, _hub = _server(make_server, config_dir, core)
    result = await _call(server, "hub_subscribe", channel_id=152, exclude=["курс"])
    assert result.is_error and "exclude — только вместе с keywords" in text_of(result)
    result = await _call(server, "hub_subscribe", channel_id=152, mode="personal", keywords=["штраф"])
    assert result.is_error and "every_message и mode" in text_of(result)
    # у подписки на проект правило Ф25-1 прежнее
    result = await _call(server, "hub_subscribe", keywords=["штраф"])
    assert result.is_error and 'mode="personal"' in text_of(result)
    assert core.seen == []


async def test_b8_subscriptions_show_channel_keys(config_dir, make_server):
    rows = [{**CHAN, "keywords": KW}, {**CHAN, "id": 5, "channel_id": 153, "keywords": None}]
    core = Core({("GET", "/subs"): (200, {"items": [], "channels": rows})})
    server, _hub = _server(make_server, config_dir, core)
    lines = text_of(await _call(server, "hub_subscriptions")).splitlines()
    with_keys = next(x for x in lines if "беседа #152 " in x)
    plain = next(x for x in lines if "беседа #153 " in x)
    assert with_keys.endswith(" · ключи 2, минус 1")
    assert "ключи" not in plain


async def test_b5_download_failed_twice_says_retry_after(
    db, tmp_path, config_dir, make_server, public_app, clock
):
    transport = _Counting(public_app)
    world = _World(db, tmp_path)
    media_id = world.attach("a.pdf", size=10)
    world.set(media_id, status="failed", attempts=4)
    # служба качает и снова не может — на первом же сне ожидания
    waits = _Clock(lambda n: world.set(media_id, status="failed"))
    server, _hub = _download_server(world, config_dir, make_server, transport, clock=waits)
    result = await _download(server, media_id)
    assert result.is_error and "не удалось загрузить из источника" in text_of(result)
    first_ms = int(clock() * 1000)
    clock.advance_ms(5 * 60 * 1000)
    result = await _download(server, media_id)
    assert not result.is_error
    retry = first_ms + 10 * 60 * 1000
    assert text_of(result) == (
        f"media_id={media_id}: не удалось загрузить из источника — повтори "
        f"hub_download(media_id={media_id}) после {fmt.fmt_ts(retry)}."
    )
    assert world.row(media_id)["status"] == "failed"
