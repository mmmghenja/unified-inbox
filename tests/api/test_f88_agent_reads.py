"""Ф88-2: агенту владельца (bearer ровно `human`) — чтение переписки, поиск и сводка дня проекта, чтобы его модель
брала их по запросу. Агент проходит каждую ручку `AGENT_READS` (не 401/403); бот `human_bot`, ключ терминала —
403; токен сессии с `read` — как раньше. Агент видит, как кука, все проекты."""

import re

import pytest

from . import test_hub_sessions as base
from .agent_hands import AGENT_READS
from .test_f56_queue import _agent, _bot
from .test_f88_agent_session_hands import _term
from .test_hub_sessions import _h

procs = base.procs
ui = base.ui
world = base.world

API = "/api/v1"

READS = [
    "/projects",
    "/projects/{p3}/channels",
    "/projects/{p3}/stages",
    "/projects/{p3}/digest",
    "/channels/{ch}",
    "/channels/{ch}/messages",
    "/channels/{ch}/threads/{thread}/messages",
    "/messages/{last}",
    "/search?q=маркетологу",
]


def test_reads_match_list(app):
    """Список исключений структурного теста совпадает с проверенными здесь ручками, и каждая существует."""

    def norm(path):
        return re.sub(r"\{[^}]+\}", "X", path.split("?")[0])

    tested = {("get", norm(API + p)) for p in READS}
    assert tested == {(m, norm(p)) for m, p in AGENT_READS}
    paths = app.openapi()["paths"]
    for method, template in AGENT_READS:
        assert method in paths[template], (method, template)


@pytest.mark.parametrize("path", READS)
async def test_agent_reads_bot_and_term_denied(client, db, world, path):
    w = world
    agent, _ = _agent(db)
    bot, _ = _bot(db)
    path = path.format(p3=w["p3"], ch=w["ch"], last=w["last"], thread="100")
    for token in (agent, w["tok"]):
        r = await client.get(f"{API}{path}", headers=_h(token))
        assert r.status_code not in (401, 403), (token == agent, r.text)
    for token in (bot, _term(db)):
        r = await client.get(f"{API}{path}", headers=_h(token))
        assert r.status_code == 403, r.text


async def test_agent_search_and_messages_see_like_ui(client, db, world):
    agent, _ = _agent(db)
    r = await client.get(f"{API}/search", params={"q": "маркетологу"}, headers=_h(agent))
    assert r.status_code == 200, r.text
    assert any(row.get("text") == "вопрос маркетологу" for row in r.json())
    r = await client.get(f"{API}/channels/{world['ch']}/messages", headers=_h(agent))
    assert r.status_code == 200, r.text
    assert "вопрос маркетологу" in r.text
    r = await client.get(f"{API}/projects", headers=_h(agent))
    assert {p["id"] for p in r.json()} >= {world["p3"], world["p4"]}
