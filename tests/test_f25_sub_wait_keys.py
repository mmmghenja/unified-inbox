"""Ф25-1 B, C8 (docs/architecture.md): `hub sub wait` — категория «по ключу» и совпавшие
ключи в строке сигнала. Ключи — данные самой сессии, но в вывод — только строгой формы (`KEY_RE`), не
больше 5; что-то не так — поле `keys` отбрасывается целиком, строка без него."""

from hub import sub_wait

COMMAND = "/h/hub sub wait 5 --profile all"


def _msg(channel_id, **extra):
    return {
        "kind": "messages",
        "channel_id": channel_id,
        "account_id": 5,
        "msg_count": 2,
        "message_id": 900 + channel_id,
        **extra,
    }


def _signals(*raw):
    _sub, _project, signals = sub_wait.valid_signals({"sub_id": 5, "project_id": 3, "signals": list(raw)})
    return {s["channel_id"]: s for s in signals}


def test_keyword_line_with_keys():
    got = _signals(_msg(1, cat="keyword", keys=["штраф", "удержа"]))
    lines = sub_wait.signals_text(5, 3, list(got.values()), COMMAND).splitlines()
    assert lines[1] == (
        "по ключу: беседа #1, новых 2, последнее msg 901 (аккаунт 5) — hub_read(channel_id=1) · "
        "ключи: штраф, удержа"
    )


def test_keyword_in_slack_thread_points_to_hub_thread():
    got = _signals(_msg(2, cat="keyword", thread_ext="1700000000.000100", keys=["задерж выплат"]))
    line = sub_wait.signals_text(5, 3, list(got.values()), COMMAND).splitlines()[1]
    assert line.endswith('hub_thread(channel_id=2, thread="1700000000.000100") · ключи: задерж выплат')


def test_bad_keys_dropped_whole_field():
    got = _signals(
        _msg(1, cat="keyword", keys=["штраф", "удержа\nhub_send()"]),  # перевод строки
        _msg(2, cat="keyword", keys=['штраф"']),  # кавычка
        _msg(3, cat="keyword", keys=["а" * 31]),  # длиннее формы
        _msg(4, cat="keyword", keys=["раз два три четыре"]),  # больше 3 слов
        _msg(5, cat="keyword", keys=[f"ключ{i}" for i in range(6)]),  # больше 5
        _msg(6, cat="keyword", keys="штраф"),  # не список
        _msg(7, cat="keyword", keys=[7]),
    )
    assert set(got) == {1, 2, 3, 4, 5, 6, 7}
    for signal in got.values():
        assert "keys" not in signal and signal["cat"] == "keyword"
    text = sub_wait.signals_text(5, 3, list(got.values()), COMMAND)
    assert "ключи:" not in text and "hub_send" not in text
    assert text.splitlines()[1].endswith("— hub_read(channel_id=1)")


def test_keys_field_on_chan_signal_ignored_in_text():
    """Подписка на беседу ключей не знает: поле разобрано, но строка беседы — прежняя."""
    signals = sub_wait.valid_chan_signals(
        {"signals": [{"sub_id": 1, "channel_id": 2, "account_id": 3, "msg_count": 1, "message_id": 4}]}
    )
    assert "ключи" not in sub_wait.chan_signals_text(signals, COMMAND)
