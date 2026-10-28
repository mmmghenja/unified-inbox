"""Ф44-1a: кнопки под сообщениями Telegram-ботов — форма, пределы, хранение и выдача
(`docs/architecture.md`).

Три формы одного и того же:

- **событие коннектора** — `buttons: null | {"kind": "inline", "rows": [[{"t", "k", "d"?, "pw"?, "u"?,
  "c"?}]], "cut"?: true}` и `buttons_ts` (мс);
- **строка БД** — `message.buttons_json` (канонический JSON события: `normalize`) и `message.buttons_ts`;
- **выдача ядра** — то же БЕЗ `d` (`public`): сырой callback наружу не отдаётся ни интерфейсу, ни сессиям.

Тексты кнопок и адреса — чужой ввод. Сырой callback кнопки по адресу `(message_id, row, col)` (с 1) достаёт
только код нажатия (44-1c): `find` и `callback_data`.
"""

import base64
import binascii
import json
import sqlite3

from .db import db_of, next_seq

KIND_INLINE = "inline"
KINDS = ("callback", "url", "copy", "other")

MAX_TEXT = 128
MAX_URL = 1024
MAX_COPY = 256
MAX_BUTTONS = 100
MAX_ROWS = 12
MAX_JSON_BYTES = 8 * 1024
# Данные callback у Telegram — до 64 байт (88 знаков base64); запас на будущее, но не безразмерно.
MAX_DATA_B64 = 256

_BUTTON_KEYS = frozenset({"t", "k", "d", "pw", "u", "c"})
_TOP_KEYS = frozenset({"kind", "rows", "cut"})


def dumps(buttons: dict) -> str:
    """Канонический JSON кнопок: по нему ядро сравнивает «клавиатура та же»."""
    return json.dumps(buttons, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def json_size(buttons: dict) -> int:
    return len(dumps(buttons).encode("utf-8"))


def cut_blank() -> dict:
    """П10: кнопки события вне пределов или не той формы — сообщение принимается, кнопки обнуляются с
    пометкой `cut` (клавиатура была, показать её нельзя)."""
    return {"kind": KIND_INLINE, "rows": [], "cut": True}


def _valid_button(button: object) -> bool:
    if not isinstance(button, dict) or not set(button) <= _BUTTON_KEYS:
        return False
    text, kind = button.get("t"), button.get("k")
    if not isinstance(text, str) or len(text) > MAX_TEXT or kind not in KINDS:
        return False
    extra = set(button) - {"t", "k"}
    if kind == "callback":
        data = button.get("d")
        if not isinstance(data, str) or len(data) > MAX_DATA_B64 or not extra <= {"d", "pw"}:
            return False
        if "pw" in button and button["pw"] is not True:
            return False
        try:
            base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            return False
        return True
    if kind == "url":
        url = button.get("u")
        return extra == {"u"} and isinstance(url, str) and 0 < len(url) <= MAX_URL
    if kind == "copy":
        copy = button.get("c")
        return extra == {"c"} and isinstance(copy, str) and 0 < len(copy) <= MAX_COPY
    return not extra


def _valid(buttons: dict) -> bool:
    if not set(buttons) <= _TOP_KEYS or buttons.get("kind") != KIND_INLINE:
        return False
    if "cut" in buttons and buttons["cut"] is not True:
        return False
    rows = buttons.get("rows")
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        return False
    total = 0
    for row in rows:
        if not isinstance(row, list) or not row:
            return False
        total += len(row)
        if total > MAX_BUTTONS or not all(_valid_button(b) for b in row):
            return False
    if not rows and not buttons.get("cut"):
        return False
    return json_size(buttons) <= MAX_JSON_BYTES


def normalize(buttons: dict | None) -> str | None:
    """Кнопки события -> `buttons_json` строки. `None` — кнопок нет. Не та форма или сверх пределов — НЕ отказ
    события (П10): `cut_blank()`."""
    if buttons is None:
        return None
    return dumps(buttons if _valid(buttons) else cut_blank())


def loads(buttons_json: str | None) -> dict | None:
    """`buttons_json` строки -> словарь в форме события (с `d`); пусто или битая строка — `None`."""
    if not buttons_json:
        return None
    try:
        data = json.loads(buttons_json)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        return None
    return data


def public(buttons_json: str | None) -> dict | None:
    """Выдача ядра (2.4): `{kind, rows: [[{t, k, u?, c?, pw?}]], cut?}` или `None`. Поле `d` не отдаётся
    НИКОГДА — список ключей здесь разрешительный."""
    data = loads(buttons_json)
    if data is None:
        return None
    rows: list[list[dict]] = []
    for row in data["rows"]:
        if not isinstance(row, list):
            return None
        out_row: list[dict] = []
        for button in row:
            if not isinstance(button, dict):
                return None
            out = {"t": button.get("t") or "", "k": button.get("k") if button.get("k") in KINDS else "other"}
            if out["k"] == "url" and isinstance(button.get("u"), str):
                out["u"] = button["u"]
            if out["k"] == "copy" and isinstance(button.get("c"), str):
                out["c"] = button["c"]
            if out["k"] == "callback" and button.get("pw") is True:
                out["pw"] = True
            out_row.append(out)
        rows.append(out_row)
    result: dict = {"kind": data.get("kind") or KIND_INLINE, "rows": rows}
    if data.get("cut") is True:
        result["cut"] = True
    return result


# --- запись (зовёт `ingest._apply_message`) -----------------------------------------------------------------


def store(
    conn: sqlite3.Connection,
    schema: str,
    message_id: int,
    *,
    old_json: str | None,
    old_ts: int | None,
    buttons: dict | None,
    buttons_ts: int,
    fresh: bool = False,
) -> int:
    """Правило 2.3: снимок кнопок с отметкой `buttons_ts` (мс) -> строка сообщения. Возвращает новый `seq`
    сообщения, если клавиатура изменилась; иначе 0.

    - отметка меньше записанной — старый снимок сверки, ничего;
    - JSON тот же — отметка двигается вперёд, `seq` не меняется (П5);
    - JSON другой — оба столбца и новый `seq`. `fresh` — строка только что вставлена этим же событием: её
      `seq` уже новый, второй не нужен.

    Истории правок, «(ред.)», сигналов подписчикам и пересчёта ключей это не даёт — их здесь просто нет.
    Скрытое, очищенное, tombstone и старше ретенции отсекает вызывающий."""
    if old_ts is not None and buttons_ts < old_ts:
        return 0
    new_json = normalize(buttons)
    if new_json == old_json:
        if old_ts is None or buttons_ts > old_ts:
            conn.execute(
                f"UPDATE {schema}.message SET buttons_ts = ? WHERE id = ?",  # noqa: S608 — схема доверенная
                (buttons_ts, message_id),
            )
        return 0
    if fresh:
        conn.execute(
            f"UPDATE {schema}.message SET buttons_json = ?, buttons_ts = ? WHERE id = ?",  # noqa: S608
            (new_json, buttons_ts, message_id),
        )
        return 0
    seq = next_seq(conn)
    conn.execute(
        f"UPDATE {schema}.message SET buttons_json = ?, buttons_ts = ?, seq = ? WHERE id = ?",  # noqa: S608
        (new_json, buttons_ts, seq, message_id),
    )
    return seq


# --- для нажатия (44-1c) -----------------------------------------------------------------------------------


def find(conn: sqlite3.Connection, message_id: int, row: int, col: int) -> dict | None:
    """Кнопка сообщения по адресу `(message_id, row, col)` — ряд и место с 1 — в форме события, С сырым `d`.
    Только для кода нажатия: наружу этот словарь не отдавать. Нет сообщения, оно скрыто или очищено, нет
    кнопки на этом месте — `None`."""
    schema = db_of(message_id)
    found = conn.execute(
        f"SELECT buttons_json FROM {schema}.message WHERE id = ? AND hidden = 0 AND purged = 0",  # noqa: S608
        (message_id,),
    ).fetchone()
    data = loads(found[0]) if found is not None else None
    if data is None or row < 1 or col < 1:
        return None
    rows = data["rows"]
    if row > len(rows) or not isinstance(rows[row - 1], list) or col > len(rows[row - 1]):
        return None
    button = rows[row - 1][col - 1]
    return button if isinstance(button, dict) else None


def callback_data(button: dict) -> bytes | None:
    """Сырые данные callback кнопки (`find`) для `GetBotCallbackAnswerRequest`; не callback, кнопка с паролем
    или битые данные — `None`."""
    if button.get("k") != "callback" or button.get("pw") is True or not isinstance(button.get("d"), str):
        return None
    try:
        return base64.b64decode(button["d"], validate=True)
    except (binascii.Error, ValueError):
        return None
