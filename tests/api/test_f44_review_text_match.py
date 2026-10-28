"""Находка 1: текст кнопки в выдаче сессии (MCP `fmt.buttons_text` -> `one_line`) заменяет символы категорий
Cc/Cf/Zl/Zp пробелом, а ядро сверяет `text` с сохранённым ДОСЛОВНО. Кнопку с эмодзи на ZWJ (U+200D — Cf),
переводом строки, LRM/RLM сессия нажать не может никогда: вечный 409 `stale_button` «перечитай сообщение»."""
import re

import pytest

from api import test_f44_presses as f44
from api import test_hub_sessions as base
from api.test_hub_sessions import A
from hub.mcp_server import format as fmt

procs = base.procs
ui = base.ui
world = base.world
ic = f44.ic


@pytest.mark.parametrize("label", ["👨‍💻 Профиль", "Да\nточно", "‏תשלום"])
async def test_session_can_press_button_it_was_shown(client, ui, db, clock, world, label):  # noqa: F811
    w = world
    kb = {"kind": "inline", "rows": [[{"t": label, "k": "callback", "d": f44._d(1)}]]}
    mid = f44._bot_msg(db, w, clock, kb=kb)
    r = await ui.get(f"{f44.API}/messages/{mid}")
    assert r.status_code == 200, r.text
    line = fmt.buttons_text(r.json()["buttons"])
    shown = re.search(r"\[1\.1\] «(.*)»", line).group(1)  # то, что видит сессия в hub_read
    made = await f44._press(client, w, mid, f44._ident(A), 1, text=shown, expect=201)
    assert made["press"]["status"] == "pending"
