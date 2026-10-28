"""Ф95-1 (B.2): ответ `hub_human` одной строкой называет правило почты, заведённое заявкой по домену
регистрации; правила нет (запретный домен, уже было) — строки нет. Ядро — подменный транспорт."""

from .test_f54_mcp_human import ARGS, Core, _call, _req, _server, text_of, workdir  # noqa: F401

LINE = "на почтовые ящики пойдут в проект"


async def test_answer_names_rule(config_dir, make_server, workdir):  # noqa: F811
    one = {
        "domain": "jobs.example",
        "project_id": 5,
        "project_name": "Hooli",
        "rules": [{"rule_id": 31, "account_id": 6}],
    }
    core = Core(str(workdir), create=(201, {"request": _req(), "created": True, "mail_rule": one}))
    server, _hub = _server(make_server, config_dir, core)
    text = text_of(await _call(server, "hub_human", **ARGS))
    assert (
        "Письма с @jobs.example на почтовые ящики пойдут в проект «Hooli» (правило #31); не выйдет регистрация — "
        "правило снимется." in text
    )
    # Два ящика — оба номера.
    two = {**one, "rules": [{"rule_id": 31, "account_id": 6}, {"rule_id": 32, "account_id": 3}]}
    core.create = (201, {"request": _req(), "created": True, "mail_rule": two})
    text = text_of(await _call(server, "hub_human", **ARGS))
    assert "(правила #31, #32)" in text


async def test_no_rule_no_line(config_dir, make_server, workdir):  # noqa: F811
    for payload in (
        {"request": _req(), "created": True, "mail_rule": None},
        {"request": _req(), "created": True},  # старое ядро без поля
        {"request": _req(), "created": False, "mail_rule": None},
    ):
        core = Core(str(workdir), create=(201, payload))
        server, _hub = _server(make_server, config_dir, core)
        text = text_of(await _call(server, "hub_human", **ARGS))
        assert "Заявка «нужен человек» #12" in text and LINE not in text
