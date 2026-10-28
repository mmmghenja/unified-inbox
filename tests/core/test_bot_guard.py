"""Ф63-2: страховка переписки сессий с ботами — `bot_guard.risky` и признак беседы с ботом (1.3, П4–П6)."""

import json

import pytest

from hub.core import bot_guard
from hub.core.bot_guard import DELETE, PAYMENT, PDN, SECRET


@pytest.mark.parametrize(
    "text",
    [
        "/start",
        "/help",
        "Да",
        "Нет",
        "Покажи тарифы",
        "Привет! Что ты умеешь?",
        "12",
    ],
)
def test_harmless(text):
    assert bot_guard.risky(text) is None


@pytest.mark.parametrize(
    ("text", "category"),
    [
        # команды удаления, с адресом бота и без
        ("/stop", DELETE),
        ("/stop@x_bot", DELETE),
        ("/delete всё", DELETE),
        ("/Unsubscribe", DELETE),
        # слова
        ("Удалите мой аккаунт", DELETE),
        ("заблокировать", DELETE),
        ("Оплатить", PAYMENT),
        ("купить подписку", PAYMENT),
        ("перевод текста", PAYMENT),  # осознанное ложное срабатывание
        ("99 ₽", PAYMENT),
        ("Buy now", PAYMENT),
        ("мой пароль", SECRET),
        ("PIN", SECRET),
        ("код из смс", SECRET),
        ("паспорт", PDN),
        ("ИНН", PDN),
        # цифры (только свой текст)
        ("4276 1234 5678 9012", PAYMENT),
        ("4276123456789012", PAYMENT),
        ("+7 900 000-00-00", PDN),
        ("89000000000", PDN),
        ("123 456", SECRET),
        ("12-34-56", SECRET),
        ("1234", SECRET),
        ("123", SECRET),  # CVV
        ("код a1b2c3", SECRET),
        ("пишите на me@example.com", PDN),
    ],
)
def test_own_text(text, category):
    assert bot_guard.risky(text) == category


def test_context_counts_only_words_and_signs():
    """П4: «Да» под вопросом бота о списании — оплата; цифры в сообщении бота ничего не значат."""
    assert bot_guard.risky("Да", context="Списать 990 ₽?") == PAYMENT
    assert bot_guard.risky("Да", context="Удалить аккаунт?") == DELETE
    assert bot_guard.risky("Да", context="Ваш номер заказа 4276 1234 5678 9012, верно?") is None
    assert bot_guard.risky("Да", context="Чем помочь?") is None
    # Свой текст главнее контекста.
    assert bot_guard.risky("/stop", context="Списать 990 ₽?") == DELETE
    # Контекст длиннее предела не смотрится дальше `CONTEXT_MAX`.
    tail = "а" * bot_guard.CONTEXT_MAX + " оплатить"
    assert bot_guard.risky("Да", context=tail) is None


def test_empty_and_none():
    assert bot_guard.risky(None) is None
    assert bot_guard.risky("") is None
    assert bot_guard.risky(None, context="Оплатить") == PAYMENT


def test_chat_is_bot():
    bot = json.dumps({"attrs": {"tg_type": "bot"}})
    user = json.dumps({"attrs": {"tg_type": "user"}})
    assert bot_guard.chat_is_bot("telegram", "dm", "9001", bot) is True
    # Человек с названием «@x_bot» (признака нет) — не бот; признак только у Telegram и лички.
    assert bot_guard.chat_is_bot("telegram", "dm", "9002", user) is False
    assert bot_guard.chat_is_bot("telegram", "dm", "9003", json.dumps({"title": "@x_bot"})) is False
    assert bot_guard.chat_is_bot("telegram", "group", "9004", bot) is False
    assert bot_guard.chat_is_bot("slack", "dm", "D1", bot) is False
    assert bot_guard.chat_is_bot("telegram", "dm", "9005", None) is False


def test_reason_roundtrip():
    assert bot_guard.reason_category(bot_guard.press_reason(PAYMENT)) == PAYMENT
    assert bot_guard.reason_category("bot_guard:nonsense") is None
    assert bot_guard.reason_category("stale_button") is None
    assert bot_guard.reason_category(None) is None


def test_draft_chat_is_bot_by_nick_only():
    """П2: заготовка `new:` по нику на «bot» — предварительный признак; по названию беседы — никогда."""

    def meta(username, title=None):
        value = {"attrs": {"new_to": {"kind": "tg_username", "username": username}}}
        if title is not None:
            value["title"] = title
        return json.dumps(value)

    assert bot_guard.chat_is_bot("telegram", "dm", "new:1", meta("x_bot")) is True
    assert bot_guard.chat_is_bot("telegram", "dm", "new:2", meta("@Shop_BOT")) is True
    assert bot_guard.chat_is_bot("telegram", "dm", "new:3", meta("petr", title="@x_bot")) is False
    assert bot_guard.chat_is_bot("mail", "dm", "new:4", meta("x_bot")) is False


# --- правки после ревью (раздел 8 спеки) --------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Оплaтить",  # латинская «a»
        "oплатить",  # латинская «o» в начале
        "о п л а т и т ь",  # буквы через пробел
        "о.п.л.а.т.и.т.ь",  # буквы через точку
        "о п л a т и т ь",  # и то и другое
        "Опла" + chr(0x200B) + "тить",  # нулевой ширины внутри
        "Оплат" + chr(0x00AD) + "ить",  # мягкий перенос
        "ＢＵＹ",  # полноширинные (NFKC)
    ],
)
def test_homoglyphs_and_spacing_are_normalized(text):
    assert bot_guard.risky(text) == PAYMENT
    assert bot_guard.risky("Да", context=text) == PAYMENT


def test_homoglyphs_other_categories():
    assert bot_guard.risky("Удaлить аккаунт") == DELETE
    assert bot_guard.risky("пaроль") == SECRET
    assert bot_guard.risky("п а с п о р т") == PDN


def test_harmless_stays_harmless_after_normalization():
    assert bot_guard.risky("Help me") is None
    assert bot_guard.risky("Да, я и ты") is None


def test_context_many_messages_any_counts():
    assert bot_guard.risky("Да", context=["Чем помочь?", "Списать 990 ₽?"]) == PAYMENT
    assert bot_guard.risky("Да", context=["Чем помочь?", "Ответьте Да или Нет"]) is None
    assert bot_guard.risky("Да", context=[]) is None


@pytest.mark.parametrize(
    "text", ["/start ref_a1b2c3", "/start 12345", "/start@x_bot abc123def", "/start AbCdEf12"]
)
def test_start_deeplink_is_not_secret(text):
    assert bot_guard.risky(text) is None


def test_start_deeplink_other_checks_stay():
    # Не диплинк (два параметра) — прежние проверки.
    assert bot_guard.risky("/start ref a1b2c3") == SECRET
    # Слова в параметре и длинные цифры — по-прежнему.
    assert bot_guard.risky("/start pay_1") == PAYMENT
    assert bot_guard.risky("/start 4276123456789012") == PAYMENT
    # Код после другой команды — секрет.
    assert bot_guard.risky("/code a1b2c3") == SECRET
