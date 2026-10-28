"""Ф38-3 C (docs/architecture.md): строка ответа `hub_subscribe` о других линиях роли с другими
настройками (`other_lines` ядра) — номер и режим линии, только ЧИСЛА ключей, без имён сессий. Ядро —
подменный транспорт."""

from .conftest import text_of
from .test_mcp_chan_subs import SUB, Core, _call, _server

ROLE = "исследователь"
MINE = {**SUB, "role": ROLE, "role_no": 1, "accounts": {"only": [8]}}
FREE = {
    "id": 21,
    "role": ROLE,
    "role_no": 2,
    "session_name": "ЧУЖОЕ ИМЯ",
    "mode": "messages",
    "msg_filter": "personal",
    "keys": {"keys": 48, "exclude": 13},
    "accounts": {"except": [8]},
    "live": False,
    "ended_ts": 5,
}
LIVE = {**FREE, "id": 45, "role_no": 3, "mode": "cards", "keys": None, "accounts": None, "live": True}
LIVE["ended_ts"] = None
HINT = (
    f"У роли «{ROLE}» есть другие линии с другими настройками: №2 — лично мне + ключи 48, минус 13 · кроме "
    "аккаунтов 8 (свободна); №3 — карточки (слушает #45). Номер в вызове не задан — взят наименьший "
    f'свободный (№1). Если настройки не твои: hub_unsubscribe(), затем hub_subscribe(role="{ROLE}", '
    "number=<свой>) — или задай mode, keywords и accounts в вызове явно."
)


def _ok(sub: dict, **extra) -> tuple[int, dict]:
    return 200, {"sub": sub, "created": True, "backfilled": 0, **extra}


async def test_hint_line_after_accounts_line(config_dir, make_server):
    core = Core({("POST", "/subs"): _ok(MINE, other_lines=[FREE, LIVE])})
    server, _hub = _server(make_server, config_dir, core)
    lines = text_of(await _call(server, "hub_subscribe", role=ROLE)).splitlines()
    assert HINT in lines
    acc = next(i for i, line in enumerate(lines) if line.startswith("Аккаунты: только 8"))
    assert lines[acc + 1] == HINT
    assert "ЧУЖОЕ ИМЯ" not in "\n".join(lines)  # имён сессий в подсказке нет


async def test_only_counts_of_keys_no_key_words(config_dir, make_server):
    """Даже если ядро вдруг отдало бы сами ключи — в строке только числа."""
    leaky = {**FREE, "keywords": {"keys": ["штраф"], "exclude": ["вакансия"]}}
    core = Core({("POST", "/subs"): _ok(MINE, other_lines=[leaky])})
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscribe", role=ROLE))
    hint = next(line for line in text.splitlines() if line.startswith("У роли «"))
    assert "№2 — лично мне + ключи 48, минус 13 · кроме аккаунтов 8 (свободна)." in hint
    assert "штраф" not in text and "вакансия" not in text


async def test_no_line_when_list_is_empty_or_absent(config_dir, make_server):
    core = Core({("POST", "/subs"): _ok(MINE, other_lines=[])})
    server, _hub = _server(make_server, config_dir, core)
    assert "другие линии" not in text_of(await _call(server, "hub_subscribe", role=ROLE))
    core.routes[("POST", "/subs")] = _ok(MINE)  # ядро старше — поля нет
    assert "другие линии" not in text_of(await _call(server, "hub_subscribe", role=ROLE))
    # кривое значение и строки без номера не печатаются
    for broken in ("№2", {"role_no": 2}, [None, "x", {"id": 5}, {"role_no": 0}, {"role_no": True}]):
        core.routes[("POST", "/subs")] = _ok(MINE, other_lines=broken)
        assert "другие линии" not in text_of(await _call(server, "hub_subscribe", role=ROLE)), broken


async def test_own_direct_still_gets_the_hint(config_dir, make_server):
    """Свой режим `direct`, у другой линии — нет: настройки разные, подсказка нужна (строки «Аккаунты»
    нет)."""
    mine = {**SUB, "role": ROLE, "role_no": 2, "mode": "direct"}
    core = Core({("POST", "/subs"): _ok(mine, other_lines=[{**LIVE, "role_no": 1}])})
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_subscribe", role=ROLE, mode="direct"))
    assert "Аккаунты:" not in text
    assert (
        f"У роли «{ROLE}» есть другие линии с другими настройками: №1 — карточки (слушает #45). Номер в "
        "вызове не задан — взят наименьший свободный (№2)."
    ) in text
