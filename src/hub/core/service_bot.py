"""Служебная личка с ботом хаба (Ф54-2, поправка П2 спеки `docs/architecture.md`).

Бот Telegram `hub-human-bot` шлёт владельцу заявки «нужен человек» с кнопками. Аккаунты Telegram владельца подключены к
хабу, поэтому без этой границы личка с ботом стала бы обычной беседой: сессии прочли бы в ней тексты и
скриншоты всех заявок и смогли бы нажимать кнопки `hub_press` — отвечать сами себе.

- id ботов хаба у Telegram — ключ `human_bot_tg_id` в `main.meta`: МНОЖЕСТВО (цифры через запятую, в порядке
  добавления). `hub auth human-bot` через админ-ручку id добавляет; прежние не забываются — иначе после
  смены бота личка с прежним на ближайшей сверке коннектора легла бы обычной беседой.
- Приём (`ingest.apply_batch`): событие аккаунта Telegram, чья беседа — личка с любым из этих id,
  отбрасывается целиком (ни беседы, ни сообщений, ни кнопок). Личка в Telegram —
  `ChannelRef(kind="dm", ext_id=<id пользователя>)` (`connectors/telegram/convert.py::dialog_channel_ref`:
  помеченный id пользователя положителен и равен его id; у групп и каналов он отрицательный, у топика —
  `<чат>:<топик>`), так что совпадение по паре `(kind, ext_id)` другие беседы не задевает.
- Уже заведённая беседа с таким собеседником (владелец писал боту до `hub auth human-bot`) НЕ стирается
  (удаление необратимо): она закрыта bearer-токенам на всех путях чтения (`queries._channel_scope` и
  точечные проверки по `channel_ids`), а после подтверждённой привязки уходит в архив. Владельцу в интерфейсе
  беседа видна как была.
"""

import re
import sqlite3

META_KEY = "human_bot_tg_id"
CLOSED_TEXT = "служебная беседа бота хаба"
TG_SOURCE = "telegram"
_ID_RE = re.compile(r"[1-9][0-9]{0,18}")


def valid_id(value: object) -> str | None:
    """id бота у Telegram строкой цифр (положительное целое) или `None`."""
    text = str(value) if isinstance(value, int) and not isinstance(value, bool) else value
    if isinstance(text, str) and _ID_RE.fullmatch(text):
        return text
    return None


def bot_id_list(conn: sqlite3.Connection) -> list[str]:
    """id ботов хаба в порядке добавления (последний — нынешний), без повторов и мусора."""
    row = conn.execute("SELECT value FROM main.meta WHERE key = ?", (META_KEY,)).fetchone()
    out: list[str] = []
    for part in str(row[0]).split(",") if row is not None and row[0] is not None else ():
        bot = valid_id(part.strip())
        if bot is not None and bot not in out:
            out.append(bot)
    return out


def bot_ids(conn: sqlite3.Connection) -> frozenset[str]:
    return frozenset(bot_id_list(conn))


def is_bot_dm(kind: str | None, ext_id: str | None, bots: frozenset[str]) -> bool:
    return kind == "dm" and ext_id in bots


def drops_event(event: object, bots: frozenset[str]) -> bool:
    """Событие коннектора Telegram относится к личке с ботом хаба — на приёме отбрасывается."""
    if not bots:
        return False
    ref = getattr(event, "channel", None)
    return ref is not None and is_bot_dm(getattr(ref, "kind", None), getattr(ref, "ext_id", None), bots)


def account_is_telegram(conn: sqlite3.Connection, account_id: int) -> bool:
    row = conn.execute("SELECT source FROM main.account WHERE id = ?", (account_id,)).fetchone()
    return row is not None and row[0] == TG_SOURCE


def channel_ids(conn: sqlite3.Connection, bots: frozenset[str] | None = None) -> frozenset[int]:
    """id бесед хаба — личек аккаунтов Telegram с ботами хаба (обе схемы; обычно пусто или одна-две)."""
    bots = bot_ids(conn) if bots is None else bots
    if not bots:
        return frozenset()
    accounts = conn.execute("SELECT id, db FROM main.account WHERE source = ?", (TG_SOURCE,)).fetchall()
    marks = ",".join("?" for _ in bots)
    out: set[int] = set()
    for account_id, schema in accounts:
        if schema not in ("main", "work"):
            continue
        rows = conn.execute(
            f"SELECT id FROM {schema}.channel WHERE account_id = ? AND kind = 'dm' "  # noqa: S608
            f"AND ext_id IN ({marks})",
            (account_id, *sorted(bots)),
        ).fetchall()
        out.update(row[0] for row in rows)
    return frozenset(out)


def is_service_channel(conn: sqlite3.Connection, schema: str, channel_id: int) -> bool:  # noqa: ARG001
    """Беседа `channel_id` — личка аккаунта Telegram с ботом хаба."""
    return channel_id in channel_ids(conn)


def account_bot_ids(conn: sqlite3.Connection, account_id: int) -> list[str]:
    """Для выдачи коннектору: личные диалоги аккаунта Telegram, которые грузить не нужно (боты хаба)."""
    return bot_id_list(conn) if account_is_telegram(conn, account_id) else []


def set_bot_id(conn: sqlite3.Connection, value: object, *, now_ms: int, archive: bool = False) -> dict:
    """Добавить id бота хаба в множество (внутри `Db.write()`); прежние id остаются. С этой записи личка
    с ботом на приёме отбрасывается, а существующая закрыта bearer-токенам. `archive=True` (привязка
    подтверждена) — существующие лички с ЭТИМ ботом уходят в архив; сообщения не стираются.
    Возвращает `{tg_id, tg_ids, closed}`: `closed` — сколько бесед с этим ботом есть в хабе."""
    from . import channels as channels_mod

    bot = valid_id(value)
    if bot is None:
        raise ValueError("tg_id: положительное целое — id бота у Telegram")
    known = [item for item in bot_id_list(conn) if item != bot] + [bot]  # нынешний — последним
    conn.execute(
        "INSERT INTO main.meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (META_KEY, ",".join(known)),
    )
    own = sorted(channel_ids(conn, frozenset({bot})))
    if archive:
        for channel_id in own:
            channels_mod.update_channel(conn, channel_id, now_ms=now_ms, archived=True)
    return {"tg_id": bot, "tg_ids": known, "closed": len(own)}
