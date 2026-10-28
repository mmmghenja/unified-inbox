"""Ф44-1a (docs/architecture.md): `convert_message` собирает
ТОЛЬКО инлайн-кнопки (`ReplyInlineMarkup`) в поле события `buttons` и всегда кладёт `buttons_ts` — сырой
`edit_date` (и при `edit_hide`), иначе `date`. Reply-клавиатуры не собираются вовсе. Сверх пределов —
обрезать и поставить `cut`. Событие проходит модель ядра."""

import base64
import json

from telethon.tl import types

from hub.connectors.telegram import convert
from hub.core import buttons as core_buttons
from hub.core.events import parse_event

from . import helpers as h

DATE_MS = 1_700_000_000_000
EDIT_MS = 1_700_000_100_000


def _ctx(entity) -> convert.ConvertContext:
    channel = convert.dialog_channel_ref(h.peer_of(entity), entity, me_id=h.ME_ID)
    return convert.ConvertContext(
        channel=channel, channel_kind_for_sender=channel["kind"], me_id=h.ME_ID, entities={}
    )


def _btn(text: str, btype) -> types.KeyboardInlineButton:
    return types.KeyboardInlineButton(text=text, type=btype)


def _inline(*rows) -> types.ReplyInlineMarkup:
    return types.ReplyInlineMarkup(rows=[types.KeyboardInlineButtonRow(buttons=list(r)) for r in rows])


def _convert(markup, **kwargs) -> dict:
    group = h.make_channel(300, title="Группа с ботом")
    msg = h.make_message(
        5, h.peer_of(group), message="выбери", date_ms=DATE_MS, reply_markup=markup, **kwargs
    )
    event = convert.convert_message(msg, _ctx(group))
    parse_event({"type": "upsert", **event})  # событие проходит модель ядра
    return event


def test_all_inline_kinds():
    markup = _inline(
        [
            _btn("Ключ", types.InlineButtonTypeCallback(data=b"k:\x00\xff")),
            _btn("Сайт", types.InlineButtonTypeUrl(url="https://example.com/a?b=1")),
            _btn("Код", types.InlineButtonTypeCopy(copy_text="PROMO-1")),
        ],
        [
            _btn("Удалить", types.InlineButtonTypeCallback(data=b"del", requires_password=True)),
            _btn("Войти", types.InlineButtonTypeUrlAuth(url="https://auth.example/x", button_id=1)),
            _btn("Приложение", types.InlineButtonTypeWebView(url="https://app.example/x")),
        ],
        [
            _btn("Инлайн", types.InlineButtonTypeSwitchInline(query="q")),
            _btn("Купить", types.InlineButtonTypeBuy()),
            _btn("Игра", types.InlineButtonTypeGame()),
            _btn("Профиль", types.InlineButtonTypeUserProfile(user_id=types.InputUserSelf())),
            _btn("Нельзя", types.InlineButtonTypeDisabled()),
        ],
    )
    event = _convert(markup)
    assert event["buttons"] == {
        "kind": "inline",
        "rows": [
            [
                {"t": "Ключ", "k": "callback", "d": base64.b64encode(b"k:\x00\xff").decode()},
                {"t": "Сайт", "k": "url", "u": "https://example.com/a?b=1"},
                {"t": "Код", "k": "copy", "c": "PROMO-1"},
            ],
            [
                {"t": "Удалить", "k": "callback", "d": base64.b64encode(b"del").decode(), "pw": True},
                # адрес url_auth и web_view не кладётся — это не обычные ссылки
                {"t": "Войти", "k": "other"},
                {"t": "Приложение", "k": "other"},
            ],
            [{"t": t, "k": "other"} for t in ("Инлайн", "Купить", "Игра", "Профиль", "Нельзя")],
        ],
    }
    assert event["buttons_ts"] == DATE_MS
    # ядро принимает собранное как есть (не обнуляет с `cut`)
    assert json.loads(core_buttons.normalize(event["buttons"])) == event["buttons"]


def test_reply_keyboards_and_no_markup_give_null_but_ts_is_always_there():
    reply = types.ReplyKeyboardMarkup(
        rows=[
            types.KeyboardButtonRow(buttons=[types.KeyboardButton(text="Да", type=types.ButtonTypeDefault())])
        ],
        single_use=True,
    )
    for markup in (
        None,
        reply,
        types.ReplyKeyboardHide(),
        types.ReplyKeyboardForceReply(),
        types.ReplyInlineMarkup(rows=[]),
    ):
        event = _convert(markup)
        assert event["buttons"] is None, markup
        assert event["buttons_ts"] == DATE_MS


def test_buttons_ts_is_raw_edit_date_even_with_edit_hide():
    markup = _inline([_btn("Ключ", types.InlineButtonTypeCallback(data=b"k"))])
    shown = _convert(markup, edit_date_ms=EDIT_MS)
    assert (shown["edited_ts"], shown["buttons_ts"]) == (EDIT_MS, EDIT_MS)
    # правку одной клавиатуры Telegram помечает `edit_hide`: «(ред.)» нет, а отметка кнопок — есть
    hidden = _convert(markup, edit_date_ms=EDIT_MS, edit_hide=True)
    assert (hidden["edited_ts"], hidden["buttons_ts"]) == (None, EDIT_MS)
    assert hidden["buttons"] == shown["buttons"]


def test_service_message_has_no_buttons():
    group = h.make_channel(300, title="Группа")
    msg = h.make_message(6, h.peer_of(group), date_ms=DATE_MS, action=types.MessageActionPinMessage())
    event = convert.convert_message(msg, _ctx(group))
    assert (event["buttons"], event["buttons_ts"]) == (None, DATE_MS)


def test_limits_cut():
    cb = types.InlineButtonTypeCallback(data=b"x")
    # текст длиннее 128 — обрезан
    event = _convert(_inline([_btn("я" * 200, cb)]))
    assert event["buttons"]["cut"] is True and event["buttons"]["rows"][0][0]["t"] == "я" * 128
    # длинный адрес и текст копирования не обрезаются (обрезанное — другое): кнопка видна, не действует
    event = _convert(
        _inline(
            [
                _btn("Сайт", types.InlineButtonTypeUrl(url="https://e.com/" + "a" * 1024)),
                _btn("Код", types.InlineButtonTypeCopy(copy_text="c" * 257)),
                _btn("Много", types.InlineButtonTypeCallback(data=b"z" * 193)),
            ]
        )
    )
    assert event["buttons"] == {
        "kind": "inline",
        "rows": [[{"t": "Сайт", "k": "other"}, {"t": "Код", "k": "other"}, {"t": "Много", "k": "other"}]],
        "cut": True,
    }
    # рядов больше 12
    event = _convert(_inline(*[[_btn(f"r{i}", cb)] for i in range(15)]))
    assert len(event["buttons"]["rows"]) == 12 and event["buttons"]["cut"] is True
    # кнопок больше 100
    event = _convert(_inline(*[[_btn(f"b{i}.{j}", cb) for j in range(11)] for i in range(10)]))
    assert sum(len(r) for r in event["buttons"]["rows"]) == 100 and event["buttons"]["cut"] is True
    # весь JSON больше 8 КиБ — ряды с конца прочь
    url = types.InlineButtonTypeUrl(url="https://e.com/" + "ю" * 1000)
    event = _convert(_inline(*[[_btn("ю" * 128, url), _btn("ю" * 128, url)] for _ in range(6)]))
    assert event["buttons"]["cut"] is True and 1 <= len(event["buttons"]["rows"]) < 6
    assert len(core_buttons.dumps(event["buttons"]).encode()) <= core_buttons.MAX_JSON_BYTES
    # всё обрезанное ядро принимает как есть
    assert json.loads(core_buttons.normalize(event["buttons"])) == event["buttons"]
    # на пределе — без `cut`
    event = _convert(_inline(*[[_btn("я" * 128, cb)] for _ in range(12)]))
    assert "cut" not in event["buttons"] and len(event["buttons"]["rows"]) == 12


def test_limits_match_core():
    assert (
        convert._BTN_TEXT_MAX,
        convert._BTN_URL_MAX,
        convert._BTN_COPY_MAX,
        convert._BTN_COUNT_MAX,
        convert._BTN_ROWS_MAX,
        convert._BTN_JSON_MAX,
    ) == (
        core_buttons.MAX_TEXT,
        core_buttons.MAX_URL,
        core_buttons.MAX_COPY,
        core_buttons.MAX_BUTTONS,
        core_buttons.MAX_ROWS,
        core_buttons.MAX_JSON_BYTES,
    )
    assert len(base64.b64encode(b"z" * convert._BTN_DATA_MAX)) <= core_buttons.MAX_DATA_B64


def test_deterministic():
    markup = _inline([_btn("Ключ", types.InlineButtonTypeCallback(data=b"k"))])
    assert _convert(markup) == _convert(markup)
