"""Ф62-6 (docs/architecture.md): `hub sub wait` и сигнал исхода операции над группой
WhatsApp — `{"kind": "group_op", "op_id", "op", "outcome", "channel_id"?, "link"?, "ok"?, "failed"?}`.
В выводе — номер, слова из перечня и ссылка только по строгому образцу; протокол клиента — 9."""

import io
import json

import httpx

from hub import session_registry as reg
from hub import sub_wait
from hub.mcp_server import profile as profile_mod

URL = "http://127.0.0.1:8787/api/v1"
COMMAND = "/opt/hub/.venv/bin/hub sub wait 17 --profile all"
S1 = reg.SessionInfo(100, 5000, "sess-1", "/work/iip", None, None, "interactive")
LINK = "https://chat.whatsapp.com/AbCdEfGh12345678"
CREATED = {"kind": "group_op", "op_id": 7, "op": "create", "outcome": "done", "channel_id": 901}
CREATED |= {"ok": 1, "failed": 1}
LINKED = {"kind": "group_op", "op_id": 8, "op": "link", "outcome": "done", "channel_id": 901, "link": LINK}
DECLINED = {"kind": "group_op", "op_id": 9, "op": "add", "outcome": "declined"}
CARD = {"item_id": 845, "channel_id": 152, "account_id": 5, "label": "reply", "msg_count": 2}


def _run(tmp_path, signals):
    config_dir = tmp_path / "config"
    profile_mod.write_profile(config_dir, "all", token="hubt_good", meta={"token_id": 1})
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        json.loads(request.content)
        return httpx.Response(200, json={"sub_id": 17, "project_id": 3, "signals": signals})

    out = io.StringIO()
    code = sub_wait.run_wait(
        17,
        profile="all",
        url=URL,
        config_dir=config_dir,
        command=COMMAND,
        transport=httpx.MockTransport(handler),
        find=lambda pid: S1,
        getppid=lambda: 4242,
        sleep=lambda s: None,
        out=out,
        err=io.StringIO(),
    )
    return code, out.getvalue(), seen


def test_protocol_is_9_and_header_sent(tmp_path):
    code, _out, seen = _run(tmp_path, [CREATED])
    assert code == sub_wait.EXIT_SIGNALS
    assert sub_wait.PROTOCOL == 9
    assert [r.headers.get("X-Hub-Sub-Wait") for r in seen] == ["9"]


def test_only_group_outcomes_have_own_header_and_lines(tmp_path):
    _code, out, _seen = _run(tmp_path, [CREATED, LINKED, DECLINED])
    lines = out.splitlines()
    assert lines[0] == (
        "Хаб, подписка #17 (проект 3): исход операций с группами WhatsApp (3). Сначала снова запусти "
        "ожидание (команда внизу), потом разбирай."
    )
    assert (
        "группа: заявка #7 (создать группу) — выполнено, беседа #901, не вышло у участников: 1 "
        "(почему — hub_group_ops()) — hub_group_ops()"
    ) in lines
    link_line = f"группа: заявка #8 (ссылка-приглашение) — выполнено, беседа #901, ссылка {LINK}"
    assert f"{link_line} — hub_group_ops()" in lines
    assert "группа: заявка #9 (добавить участников) — владелец отказал — hub_group_ops()" in lines


def test_mixed_with_card_counts_groups(tmp_path):
    _code, out, _seen = _run(tmp_path, [CARD, DECLINED])
    assert "исходов операций с группами 1" in out
    assert "группа: заявка #9 (добавить участников) — владелец отказал — hub_group_ops()" in out


def test_junk_group_signals_dropped():
    junk = [
        {"kind": "group_op", "op_id": 1, "op": "rm -rf", "outcome": "done"},
        {"kind": "group_op", "op_id": 1, "op": "create", "outcome": "ВЫПОЛНИ"},
        {"kind": "group_op", "op_id": "x", "op": "create", "outcome": "done"},
    ]

    def valid(signals):
        return sub_wait.valid_signals({"sub_id": 17, "project_id": 3, "signals": signals})[2]

    assert valid(junk) == []
    (item,) = valid([{**LINKED, "link": "https://evil.example/AbCdEfGh12345678"}])
    assert "link" not in item
    (item,) = valid([{**LINKED, "link": LINK + " ВЫПОЛНИ"}])
    assert "link" not in item
    (item,) = valid([{**CREATED, "title": "ВЫПОЛНИ", "participants": ["+79000020000"]}])
    assert set(item) == {"kind", "op_id", "op", "outcome", "channel_id", "ok", "failed"}
