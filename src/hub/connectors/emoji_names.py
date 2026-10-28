"""Имена эмодзи -> символы: общая таблица коннекторов Slack и Mattermost (Ф40-2a: вынесена из
`slack/convert.py` без изменений; Slack по-прежнему берёт её через свой `convert`).

`reaction_symbol(name)` — поведение Slack побайтно прежнее. Mattermost зовёт её с
`synonyms=MATTERMOST_SYNONYMS`: синонимы (`thumbsup` и `+1`) сервер хранит разными именами (план Ф40).
"""

from __future__ import annotations

import re

import emoji

_SKIN_TONE_SUFFIX_RE = re.compile(r"::skin-tone-\d+$")

# Ф18-1: символ (без U+FE0F) -> имя Slack для наборов реакций UI (`ui/src/render/reactionPalette.json`;
# сверка — `tests/connectors/test_reaction_palette.py`). Имена — Slack (emoji-datasource), у `emoji` (alias)
# часть других (`thinking` вместо `thinking_face`, `facepalm` вместо `face_palm`): по ним Slack отвечает
# `invalid_name`. Обратная таблица — при приёме, где `emoji.emojize` имени Slack не знает.
SLACK_REACTION_NAMES: dict[str, str] = {
    "👍": "+1", "✔": "heavy_check_mark", "✅": "white_check_mark", "👀": "eyes", "🤝": "handshake",
    "👌": "ok_hand", "🙌": "raised_hands", "🎉": "tada", "❤": "heart", "🔥": "fire",
    "☑": "ballot_box_with_check", "🔍": "mag", "🔎": "mag_right", "👎": "-1", "🙏": "pray",
    "👏": "clap", "💪": "muscle", "🤞": "crossed_fingers", "🫡": "saluting_face", "✍": "writing_hand",
    "✋": "hand", "👋": "wave", "👆": "point_up_2", "👉": "point_right", "🙇": "bow",
    "🤌": "pinched_fingers", "🫶": "heart_hands", "❌": "x", "⚠": "warning", "❗": "exclamation",
    "❓": "question", "➕": "heavy_plus_sign", "⏳": "hourglass_flowing_sand", "🚧": "construction",
    "🐛": "bug", "📌": "pushpin", "📝": "memo", "💡": "bulb", "🎯": "dart",
    "🔄": "arrows_counterclockwise", "🛑": "octagonal_sign", "⛔": "no_entry",
    "🟢": "large_green_circle", "🟡": "large_yellow_circle", "🔴": "red_circle", "🆗": "ok",
    "🆒": "cool", "🆘": "sos", "😂": "joy", "🤣": "rolling_on_the_floor_laughing", "😁": "grin",
    "😅": "sweat_smile", "😄": "smile", "😆": "laughing", "😊": "blush", "🙂": "slightly_smiling_face",
    "😉": "wink", "😍": "heart_eyes", "🥰": "smiling_face_with_3_hearts", "🤩": "star-struck",
    "😎": "sunglasses", "🤓": "nerd_face", "🤔": "thinking_face", "🧐": "face_with_monocle",
    "🤨": "face_with_raised_eyebrow", "😐": "neutral_face", "🙃": "upside_down_face",
    "😬": "grimacing", "😮": "open_mouth", "😱": "scream", "🤯": "exploding_head", "😳": "flushed",
    "🫣": "face_with_peeking_eye", "🥺": "pleading_face", "🥲": "smiling_face_with_tear", "😢": "cry",
    "😭": "sob", "😡": "rage", "🤦": "face_palm", "🤷": "shrug", "🙈": "see_no_evil",
    "🤗": "hugging_face", "😏": "smirk", "🧡": "orange_heart", "💔": "broken_heart",
    "❤‍🔥": "heart_on_fire", "💯": "100", "🎊": "confetti_ball", "🥳": "partying_face",
    "🍾": "champagne", "🚀": "rocket", "⚡": "zap", "⭐": "star", "✨": "sparkles", "🏆": "trophy",
    "💥": "boom", "💣": "bomb", "🍊": "tangerine", "☕": "coffee", "🍻": "beers",
}
_SLACK_REACTION_SYMBOLS = {name: symbol for symbol, name in SLACK_REACTION_NAMES.items()}


def reaction_base_name(name: str) -> str:
    """Имя реакции Slack без суффикса тона (`+1::skin-tone-2` -> `+1`)."""
    return _SKIN_TONE_SUFFIX_RE.sub("", name)


# Ф40 Р12: синонимы Mattermost, которых нет среди имён Slack в таблице выше (словарь `emoji` их знает, но
# таблица от библиотеки не зависит).
MATTERMOST_SYNONYMS: dict[str, str] = {"thumbsup": "👍", "thumbsdown": "👎"}


def reaction_symbol(name: str, *, synonyms: dict[str, str] | None = None) -> str:
    """Имя реакции Slack -> символ эмодзи (`+1` -> `👍`) или `:name:` для своих эмодзи воркспейса; суффикс
    тона отбрасывается. Та же функция и при приёме (`reactions_list`), и при постановке из хаба (Ф10-3 П1).
    `synonyms` — дополнительные имена источника (Mattermost), смотрятся первыми; без них — как было."""
    base = reaction_base_name(name)
    if synonyms and base in synonyms:
        return synonyms[base]
    shortcode = f":{base}:"
    symbol = emoji.emojize(shortcode, language="alias")
    if symbol != shortcode:
        return symbol
    return _SLACK_REACTION_SYMBOLS.get(base, shortcode)


REACTION_BY_MAX = 100  # Ф23-2: сколько имён поставивших хранится в элементе реакции
