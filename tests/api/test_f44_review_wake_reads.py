"""Наблюдение 3: чтение нажатий и ответа бота в ядре закрыто только скоупом `read` и самозаявленной тройкой:
токен дежурной (`mcp:wake-*`) и оркестратора, которым `hub_press`/`hub_presses` в MCP скрыты, а POST в ядре
запрещён, ответ бота по GET получают, назвав тройку автора."""
from api import test_f44_presses as f44
from api import test_hub_sessions as base
from api.test_hub_sessions import A, _h, _sess, _token

procs = base.procs
ui = base.ui
world = base.world
ic = f44.ic


async def test_wake_and_orchestrator_tokens_do_not_read_bot_answer(client, ui, ic, db, clock, world):  # noqa: F811
    w = world
    h = f44._conn_headers(db, w["acc"])
    mid = f44._bot_msg(db, w, clock)
    press = (await f44._press(client, w, mid, f44._ident(A), 1))["press"]
    assert (await ui.post(f"{f44.API}/button-presses/{press['id']}/approve")).status_code == 200
    (item,) = await f44._lease(ic, h)
    await f44._report(ic, h, item, result={"message": "ключ: SECRET-KEY-1"})
    for name in ("mcp:wake-all", "mcp:orchestrator"):
        token, _ = _token(db, name=name, kind="interactive", scopes=("read", "draft"))
        r = await client.get(f"{f44.API}/button-presses/{press['id']}", params=_sess(A), headers=_h(token))
        assert r.status_code in (403, 404), (name, r.status_code, r.text)
