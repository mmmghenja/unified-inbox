"""24.10: `hub_draft_new` от сессии с живой подпиской на проект и ролью сам подписывает её на созданную
беседу-заготовку (быстрый ответ человека — сигналом); без роли — не подписывает; подписка уже есть — одна;
предел бесед — строкой, черновик лёг. Настоящее ядро в процессе (стенд `test_f39_mcp_author`)."""

from hub.core import chan_subs

from .conftest import text_of
from .test_f39_mcp_author import ROLE, _call, stand  # noqa: F401 — фикстура стенда


def _live_chan_subs(db) -> list[int]:
    with db.reader() as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT channel_id FROM main.session_chan_sub WHERE ended_ts IS NULL ORDER BY id"
            )
        ]


def _new_channel(db) -> int:
    with db.reader() as conn:
        return conn.execute("SELECT channel_id FROM main.outbox ORDER BY id DESC LIMIT 1").fetchone()[0]


async def _draft_new(stand, to="@petr_ivanov"):  # noqa: F811
    return await _call(
        stand["server"],
        "hub_draft_new",
        project_id=stand["project"],
        account_id=stand["account"],
        to=to,
        text="Здравствуйте!",
    )


async def test_role_session_is_subscribed_to_new_channel(stand):  # noqa: F811
    result = await _call(stand["server"], "hub_subscribe", role=ROLE)
    assert not result.is_error, text_of(result)
    result = await _draft_new(stand)
    text = text_of(result)
    assert not result.is_error, text
    channel = _new_channel(stand["db"])
    assert f"Подписка на беседу #{channel} — есть (c" in text
    assert "hub sub wait --channels" in text  # внешний Claude Code: ожидание бесед ещё не идёт
    assert _live_chan_subs(stand["db"]) == [channel]
    # второй черновик в ту же заготовку нельзя; другой адресат — своя подписка, первая не дублируется
    result = await _draft_new(stand, to="@ivan_petrov")
    assert not result.is_error, text_of(result)
    assert _live_chan_subs(stand["db"]) == [channel, _new_channel(stand["db"])]


async def test_without_role_no_subscription(stand):  # noqa: F811
    result = await _call(stand["server"], "hub_subscribe")
    assert not result.is_error, text_of(result)
    result = await _draft_new(stand)
    assert not result.is_error, text_of(result)
    assert "Подписка на беседу" not in text_of(result)
    assert _live_chan_subs(stand["db"]) == []


async def test_limit_reached_is_a_line_draft_stays(stand, monkeypatch):  # noqa: F811
    monkeypatch.setattr(chan_subs, "MAX_CHAN_SUBS", 1)
    result = await _call(stand["server"], "hub_subscribe", role=ROLE)
    assert not result.is_error, text_of(result)
    result = await _call(stand["server"], "hub_subscribe", channel_id=stand["channel"])
    assert not result.is_error, text_of(result)
    result = await _draft_new(stand)
    text = text_of(result)
    assert not result.is_error, text
    channel = _new_channel(stand["db"])
    assert f"Подписка на беседу #{channel} не оформлена:" in text
    assert "Черновик #" in text
    assert _live_chan_subs(stand["db"]) == [stand["channel"]]
