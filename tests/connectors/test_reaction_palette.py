"""Ф18-1: наборы реакций UI (`ui/src/render/reactionPalette.json` — быстрый ряд и «ещё») совместимы с ядром и
коннекторами: каждый эмодзи проходит `normalize_emoji`; Telegram — только из бесплатного набора Telegram;
Slack — имя из таблицы коннектора, и при приёме это имя даёт тот же символ (иначе своя реакция после
постановки не совпала бы с операцией в пути и через 60 с стала бы «не поставилось»)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hub.connectors.emoji_names import MATTERMOST_SYNONYMS, SLACK_REACTION_NAMES, reaction_symbol
from hub.core.reactions import TELEGRAM_FREE_REACTIONS, normalize_emoji

_PALETTE_PATH = Path(__file__).resolve().parents[2] / "ui" / "src" / "render" / "reactionPalette.json"
_PALETTE = json.loads(_PALETTE_PATH.read_text("utf-8"))
_VS16 = chr(0xFE0F)


def _items(source: str) -> list[str]:
    return [*_PALETTE[source]["quick"], *_PALETTE[source]["all"]]


def test_palette_shape() -> None:
    assert set(_PALETTE) == {"telegram", "slack", "mattermost", "mail"}
    for source, sets in _PALETTE.items():
        for name in ("quick", "all"):
            items = sets[name]
            assert items and all(isinstance(e, str) and e for e in items), (source, name)
            assert len(set(items)) == len(items), (source, name, "повтор")
        assert set(sets["quick"]) <= set(sets["all"]), (source, "быстрый ряд не из полного набора")


def test_telegram_free_set_is_bot_api_list() -> None:
    assert len(TELEGRAM_FREE_REACTIONS) == 73
    assert {"🤝", "👍", "❤", "🫡", "✍", "❤‍🔥"} <= TELEGRAM_FREE_REACTIONS
    assert "✅" not in TELEGRAM_FREE_REACTIONS and "🔍" not in TELEGRAM_FREE_REACTIONS


@pytest.mark.parametrize("value", _items("telegram"))
def test_telegram_palette(value: str) -> None:
    assert normalize_emoji("telegram", value) in TELEGRAM_FREE_REACTIONS


def test_telegram_all_is_whole_free_set() -> None:
    assert {normalize_emoji("telegram", e) for e in _PALETTE["telegram"]["all"]} == TELEGRAM_FREE_REACTIONS


@pytest.mark.parametrize("value", _items("mail"))
def test_mail_palette(value: str) -> None:
    assert normalize_emoji("mail", value) == value


# Ф40-4 A5: Mattermost — набор Slack без четырёх эмодзи, которых нет на сервере 11.x.
_MM_MISSING = {"🫡", "🫶", "🫣", "❤‍🔥"}  # saluting_face, heart_hands, face_with_peeking_eye, heart_on_fire


def _key(value: str) -> str:
    return value.replace(_VS16, "")


def test_mattermost_palette_is_slack_without_four() -> None:
    mm, slack = _PALETTE["mattermost"], _PALETTE["slack"]
    assert mm["quick"] == slack["quick"]
    assert [e for e in slack["all"] if _key(e) not in _MM_MISSING] == mm["all"]
    assert len(mm["all"]) == len(slack["all"]) - len(_MM_MISSING)
    assert not {_key(e) for e in _items("mattermost")} & _MM_MISSING


@pytest.mark.parametrize("value", _items("mattermost"))
def test_mattermost_palette_round_trip(value: str) -> None:
    assert normalize_emoji("mattermost", value) == value
    key = _key(value)
    name = SLACK_REACTION_NAMES[key]  # имя эмодзи, под которым сервер Mattermost хранит реакцию
    assert _key(reaction_symbol(name, synonyms=MATTERMOST_SYNONYMS)) == key
    assert _key(reaction_symbol(f"{name}::skin-tone-3", synonyms=MATTERMOST_SYNONYMS)) == key
