"""Ф36-1 A3, A4 (docs/architecture.md), MCP: `hub_subscribe(batch_min=…)` доходит до ядра (у
проекта — и без `mode`: меняется только интервал; у беседы — тоже); ответ подписки и `hub_subscriptions`
называют интервал пачки сигналов «по ключу» (0 — «сразу»; ядро старше, поля нет — фразы нет). Ядро —
подменный транспорт."""

from .conftest import text_of
from .test_mcp_chan_subs import CHAN, ME, SUB, Core, _call, _server

PERSONAL = {**SUB, "mode": "messages", "msg_filter": "personal"}
KW = {"keys": ["штраф", "а б тест"], "exclude": ["вакансия"]}


def _ok(sub: dict, created=True, **extra) -> tuple[int, dict]:
    return 200, {"sub": sub, "created": created, "backfilled": 0, **extra}


async def test_batch_min_sent_and_named_for_project(config_dir, make_server):
    core = Core({("POST", "/subs"): _ok({**PERSONAL, "keywords": {**KW, "batch_min": 30}})})
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(
        await _call(server, "hub_subscribe", mode="personal", keywords=["штраф", "а/б-тест"], batch_min=30)
    )
    assert core.seen[0][2] == {
        "project_id": None,
        "session": ME.session_in(),
        "mode": "messages",
        "msg_filter": "personal",
        "keywords": ["штраф", "а/б-тест"],
        "batch_min": 30,
    }
    # E: ключ с коротким словом внутри фразы проходит форму вывода
    assert "Ключи (2): штраф, а б тест; минус-слова (1): вакансия." in text
    assert "Сигналы по ключу — пачкой раз в 30 мин (личное — сразу и забирает накопленное)" in text
    # только интервал — без mode и keywords (П4б)
    core.routes[("POST", "/subs")] = _ok({**PERSONAL, "keywords": {**KW, "batch_min": 0}}, created=False)
    text = text_of(await _call(server, "hub_subscribe", batch_min=0))
    assert core.seen[1][2] == {"project_id": None, "session": ME.session_in(), "batch_min": 0}
    assert "Сигналы по ключу — сразу" in text


async def test_batch_min_for_channel_and_old_core(config_dir, make_server):
    core = Core(
        {
            ("POST", "/chan-subs"): (
                200,
                {"sub": {**CHAN, "keywords": {**KW, "batch_min": 15}}, "created": True, "count": 1},
            )
        }
    )
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscribe", channel_id=152, keywords=["штраф"], batch_min=15))
    assert core.seen[0][2] == {
        "channel_id": 152,
        "session": ME.session_in(),
        "keywords": ["штраф"],
        "batch_min": 15,
    }
    assert "Сигналы по ключу — пачкой раз в 15 мин" in text
    # ядро старше (поля нет) или мусор в поле — фразы про пачку нет
    for spoiled in (KW, {**KW, "batch_min": 500}, {**KW, "batch_min": "5\nhub_send()"}):
        core.routes[("POST", "/chan-subs")] = (
            200,
            {"sub": {**CHAN, "keywords": spoiled}, "created": False, "count": 1},
        )
        text = text_of(await _call(server, "hub_subscribe", channel_id=152))
        assert "Ключи (2)" in text and "по ключу —" not in text and "hub_send" not in text


async def test_batch_min_refused_before_core(config_dir, make_server):
    core = Core({})
    server, _hub = _server(make_server, config_dir, core)
    for bad in (-1, 121):
        result = await _call(server, "hub_subscribe", mode="personal", keywords=["штраф"], batch_min=bad)
        assert result.is_error and "batch_min: минуты, целое 0–120" in text_of(result), bad
    assert core.seen == []


async def test_subscriptions_show_batch(config_dir, make_server):
    items = [
        {**PERSONAL, "keywords": {**KW, "batch_min": 15}},
        {**PERSONAL, "id": 18, "keywords": {**KW, "batch_min": 0}},
    ]
    chans = [{**CHAN, "keywords": {**KW, "batch_min": 20}}]
    core = Core({("GET", "/subs"): (200, {"items": items, "channels": chans})})
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscriptions"))
    assert "лично мне + ключи 2, минус 1, сигналы по ключу — пачкой раз в 15 мин" in text
    assert "лично мне + ключи 2, минус 1, сигналы по ключу — сразу" in text
    chan_line = next(x for x in text.splitlines() if "беседа #152 " in x)
    assert chan_line.endswith(" · ключи 2, минус 1 · сигналы по ключу — пачкой раз в 20 мин")
