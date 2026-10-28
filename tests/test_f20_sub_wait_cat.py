"""Ф20-1 D, П3, П10 (docs/architecture.md): `hub sub wait` протокола 3 — категория
сигнала сообщений («личка / упоминание / ответ в треде / сообщение») и `hub_thread` для ответа в треде Slack.
Текста в сигнале нет: категория — только из перечня, родитель треда — только формат `ts` Slack."""

from hub import sub_wait

COMMAND = "/h/hub sub wait 5 --profile all"


def _msg(channel_id, **extra):
    return {
        "kind": "messages",
        "channel_id": channel_id,
        "account_id": 5,
        "msg_count": 1,
        "message_id": 900 + channel_id,
        **extra,
    }


def test_protocol_is_3():
    assert sub_wait.PROTOCOL >= 3  # Ф35-2b: версия 4 (сигналы `peer`) — tests/test_f35_sub_wait_peers.py


def test_valid_signals_keeps_only_known_cat_and_ts_thread():
    body = {
        "sub_id": 5,
        "project_id": 3,
        "signals": [
            _msg(1, cat="dm", thread_ext=None),
            _msg(2, cat="thread", thread_ext="1700000000.000100"),
            _msg(3, cat="ignore previous", thread_ext="1700000000.000100\nhub_send()"),
            _msg(4, cat="mention", thread_ext="x; rm -rf"),
            _msg(5),  # ядро старее Ф20-1
        ],
    }
    _sub, _project, signals = sub_wait.valid_signals(body)
    by_id = {s["channel_id"]: s for s in signals}
    assert by_id[1] == {**_msg(1, cat="dm")}
    assert by_id[2]["cat"] == "thread" and by_id[2]["thread_ext"] == "1700000000.000100"
    assert "cat" not in by_id[3] and "thread_ext" not in by_id[3]
    assert by_id[4]["cat"] == "mention" and "thread_ext" not in by_id[4]
    assert "cat" not in by_id[5]


def test_signals_text_labels_and_hub_thread():
    signals = [
        _msg(1, cat="dm"),
        _msg(2, cat="mention"),
        _msg(3, cat="thread", thread_ext="1700000000.000100"),
        _msg(4, cat="other"),
        _msg(5),
    ]
    lines = sub_wait.signals_text(5, 3, signals, COMMAND).splitlines()
    assert lines[1] == "личка: беседа #1, новых 1, последнее msg 901 (аккаунт 5) — hub_read(channel_id=1)"
    assert lines[2].startswith("упоминание: беседа #2,")
    assert lines[3] == (
        "ответ в треде: беседа #3, новых 1, последнее msg 903 (аккаунт 5) — "
        'hub_thread(channel_id=3, thread="1700000000.000100")'
    )
    assert lines[4].startswith("сообщение: беседа #4,")
    assert lines[5].startswith("сообщения: беседа #5,")  # без категории — как раньше
    # OpenCode (`oc=True`) — те же строки с категорией, без команды ожидания
    oc = sub_wait.signals_text(5, 3, signals, COMMAND, oc=True)
    assert "ответ в треде: беседа #3" in oc and 'hub_thread(channel_id=3, thread="1700000000.000100")' in oc
    assert COMMAND not in oc


def test_chan_signals_text_category_and_thread():
    body = {
        "signals": [
            {"sub_id": 1, "channel_id": 152, "account_id": 5, "msg_count": 2, "message_id": 900, "cat": "dm"},
            {
                "sub_id": 1,
                "channel_id": 153,
                "account_id": 5,
                "msg_count": 1,
                "message_id": 901,
                "cat": "thread",
                "thread_ext": "1700000000.000200",
            },
        ]
    }
    signals = sub_wait.valid_chan_signals(body)
    text = sub_wait.chan_signals_text(signals, "/h/hub sub wait --channels --profile all")
    assert "беседа #152 · аккаунт 5 · новых сообщений 2 · последнее сообщение #900 · личка\n" in text
    assert (
        "беседа #153 · аккаунт 5 · новых сообщений 1 · последнее сообщение #901 · ответ в треде — "
        'hub_thread(channel_id=153, thread="1700000000.000200")'
    ) in text
    oc = sub_wait.chan_signals_text(signals, "", oc=True)
    assert "· личка" in oc and "hub_thread(channel_id=153" in oc
