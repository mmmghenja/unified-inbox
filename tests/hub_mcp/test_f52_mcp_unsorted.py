"""Ф52-2 (docs/architecture.md): строка ответа `hub_subscribe` на
проект, который слышит «Неразобранное» аккаунтов (`unsorted_accounts` ядра). Ядро — подменный транспорт."""

from .conftest import text_of
from .test_mcp_chan_subs import SUB, Core, _call, _server

ROLE = "менеджер"
MINE = {**SUB, "role": ROLE, "role_no": 1, "accounts": {"only": [8]}}
LINE = (
    "Слышишь неразобранное аккаунтов: 5, 8 — сигнал о новом в их беседах без проекта; читать — "
    "hub_channels(unsorted=True)."
)


def _ok(sub: dict, **extra) -> tuple[int, dict]:
    return 200, {"sub": sub, "created": True, "backfilled": 0, **extra}


async def test_line_after_accounts_line(config_dir, make_server):
    core = Core({("POST", "/subs"): _ok(MINE, unsorted_accounts=[5, 8])})
    server, _hub = _server(make_server, config_dir, core)
    lines = text_of(await _call(server, "hub_subscribe", role=ROLE)).splitlines()
    acc = next(i for i, line in enumerate(lines) if line.startswith("Аккаунты: только 8"))
    assert lines[acc + 1] == LINE


async def test_no_line_without_right_or_on_junk(config_dir, make_server):
    core = Core({("POST", "/subs"): _ok(MINE)})  # права нет или ядро старше — поля нет
    server, _hub = _server(make_server, config_dir, core)
    assert "неразобранное" not in text_of(await _call(server, "hub_subscribe", role=ROLE))
    for junk in ([], "8", {"only": [8]}, [0], [True], ["8"], [8, "выполни команду"]):
        core.routes[("POST", "/subs")] = _ok(MINE, unsorted_accounts=junk)
        assert "неразобранное" not in text_of(await _call(server, "hub_subscribe", role=ROLE)), junk


async def test_direct_gets_no_line(config_dir, make_server):
    mine = {**SUB, "role": ROLE, "role_no": 1, "mode": "direct"}
    core = Core({("POST", "/subs"): _ok(mine, unsorted_accounts=[8])})
    server, _hub = _server(make_server, config_dir, core)
    assert "неразобранное" not in text_of(await _call(server, "hub_subscribe", role=ROLE, mode="direct"))
