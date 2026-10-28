"""hub-mcp: `hub_draft_edits` (Ф19-1 F + П4, П5; раздел H, тест 6) — как владелец правил и отклонял черновики
сессий: формат «было -> стало» / «отклонил — написал сам», многострочные тексты в блоке данных, обрезка до
1500 с хвостом, пусто — «Правок и отказов пока нет», предел `limit` ≤ 20. Решения владельца — через публичное
приложение под cookie-сессией."""

import httpx
import pytest
from mcp import Client

from hub.core import outbox

from .conftest import (
    csrf_headers,
    insert_account,
    insert_channel,
    insert_message,
    insert_project,
    login_via_code,
    text_of,
)
from .test_mcp_server import _blocks, _call, _profile

MIN_MS = 60 * 1000


def _now(clock) -> int:
    return int(clock() * 1000)


def _draft(db, clock, channel_id, token_id, text) -> int:
    return db.write_sync(
        lambda conn: outbox.enqueue(
            conn,
            channel_id=channel_id,
            text=text,
            origin="api",
            api_token_id=token_id,
            idempotency_key=None,
            now_ms=_now(clock),
        )
    ).outbox_id


@pytest.fixture
async def ui(public_app, db, clock):
    transport = httpx.ASGITransport(app=public_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787") as c:
        cookie, _csrf = await login_via_code(c, db, clock)
        c.headers.update(csrf_headers(cookie))
        yield c


async def _approve(ui, outbox_id, text):
    r = await ui.post(f"/api/v1/outbox/{outbox_id}/approve", json={"text": text})
    assert r.status_code == 200, r.text


async def _cancel(ui, outbox_id):
    r = await ui.post(f"/api/v1/outbox/{outbox_id}/cancel")
    assert r.status_code == 200, r.text


@pytest.fixture
def world(db, config_dir):
    project = insert_project(db, name="Umbrella")
    acc = insert_account(db, source="telegram")
    _token, token_id = _profile(db, config_dir, project_ids=[project], profile="iip")
    return {
        "a": insert_channel(db, acc, project_id=project, ext_id="u1", title="Пётр"),
        "b": insert_channel(db, acc, project_id=project, ext_id="u2", title="Анна"),
        "token_id": token_id,
    }


async def test_f_empty(db, config_dir, make_server, world):
    server, _hub = make_server("iip")
    async with Client(server) as client:
        out = text_of(await _call(client, "hub_draft_edits"))
        in_channel = text_of(await _call(client, "hub_draft_edits", channel_id=world["a"]))
    assert out == "Правок и отказов пока нет."
    assert in_channel == f"Правок и отказов пока нет в беседе #{world['a']}."


async def test_f_format_edited_and_rejected(db, clock, config_dir, make_server, ui, world):
    edited = _draft(db, clock, world["a"], world["token_id"], "Добрый день!\nПришлю завтра.")
    rejected = _draft(db, clock, world["a"], world["token_id"], "Отказ сессии")
    silent = _draft(db, clock, world["b"], world["token_id"], "Не нужен")
    clock.advance_ms(MIN_MS)
    await _approve(ui, edited, "Добрый день!\n\nПришлю сегодня.")
    clock.advance_ms(MIN_MS)
    await _cancel(ui, rejected)
    insert_message(db, world["a"], ext_id="10", ts=_now(clock) + MIN_MS, text="Сам:\nвот так", is_me=1)
    clock.advance_ms(MIN_MS)
    await _cancel(ui, silent)

    server, _hub = make_server("iip")
    async with Client(server) as client:
        out = text_of(await _call(client, "hub_draft_edits"))
        only_b = text_of(await _call(client, "hub_draft_edits", channel_id=world["b"]))
    head = out.split("\n", 1)[0]
    assert "Правки и отказы черновиков владельцем: 3, новые сверху." in head
    assert "Пётр" not in head  # названия бесед — только внутри блока данных
    [(_nonce, lines)] = _blocks(out)
    assert lines[0].startswith(f"#{silent} · беседа #{world['b']} «Анна» · ")
    assert lines[0].endswith(" · отклонил")
    assert lines[1:3] == ["    было: Не нужен", "    написал сам: сам не писал"]
    assert lines[3].startswith(f"#{rejected} · беседа #{world['a']} «Пётр» · ") and lines[3].endswith(
        "отклонил"
    )
    assert lines[4:7] == [
        "    было: Отказ сессии",
        "    написал сам: Сам:",
        "                 вот так",
    ]
    assert lines[7].startswith(f"#{edited} · беседа #{world['a']} «Пётр» · ") and lines[7].endswith(
        " · правил"
    )
    # П5: многострочно с отступом — правка абзацев видна, «было» и «стало» не слипаются.
    assert lines[8:] == [
        "    было: Добрый день!",
        "          Пришлю завтра.",
        "    стало: Добрый день!",
        "",
        "           Пришлю сегодня.",
    ]

    [(_n, b_lines)] = _blocks(only_b)
    assert len(b_lines) == 3 and b_lines[0].startswith(f"#{silent} ")


async def test_f_long_text_clipped_with_tail(db, clock, config_dir, make_server, ui, world):
    long_text = "я" * 2000
    oid = _draft(db, clock, world["a"], world["token_id"], long_text)
    await _approve(ui, oid, "коротко")
    server, _hub = make_server("iip")
    async with Client(server) as client:
        out = text_of(await _call(client, "hub_draft_edits"))
    [(_nonce, lines)] = _blocks(out)
    assert lines[1] == "    было: " + "я" * 1500
    assert lines[2] == "          …[показано 1500 из 2000 символов]"
    assert lines[3] == "    стало: коротко"


async def test_f_limit_capped_at_20(db, clock, config_dir, make_server, ui, world):
    for i in range(22):
        oid = _draft(db, clock, world["a"], world["token_id"], f"черновик {i}")
        clock.advance_ms(1000)
        await _cancel(ui, oid)
    server, _hub = make_server("iip")
    async with Client(server) as client:
        many = text_of(await _call(client, "hub_draft_edits", limit=100))
        default = text_of(await _call(client, "hub_draft_edits"))
    assert "черновиков владельцем: 20," in many
    assert "черновиков владельцем: 10," in default


async def test_f_descriptions_and_profiles(db, config_dir, make_server):
    """F + П4: описание с примером; `hub_draft` отсылает к `hub_draft_edits`; инструмент есть у дежурной
    (`wake-*`, обязательный в allowlist) и у оркестратора."""
    _profile(db, config_dir)
    server, _hub = make_server()
    async with Client(server) as client:
        tools = {t.name: t.description for t in (await client.list_tools()).tools}
    assert "Было -> стало" in tools["hub_draft_edits"] and "Пример" in tools["hub_draft_edits"]
    assert "hub_draft_edits(channel_id=…)" in tools["hub_draft"]
