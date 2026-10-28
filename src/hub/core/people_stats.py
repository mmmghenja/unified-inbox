"""Статистика переписки по людям (Ф21-1, docs/architecture.md). Без LLM: всё считается из ленты за год, пересчёт раз в
час целиком.

Чтение — своим `db.reader()` в потоке (`recompute` зовётся через `asyncio.to_thread`), писатель не держится;
флаг остановки ядра проверяется между беседами — остановка посреди прохода выходит без записи. Запись —
одна `db.write_sync`: в каждом файле `DELETE FROM contact_stats` и вставка строк по контактам-людям со следом
в окне.

Что считается (кратко, подробности — в спеке):
- D1 адресат: личка (и групповая личка Slack, П2) — чужое «Владельцу»; письмо владельца — по `mail_to_json`; группы —
  личное упоминание, ответ на сообщение владельца, ответ в треде владельца. Кто кому пишет без владельца — не считается.
- П1: сообщение владельца — только отправленное (`ext_id IS NOT NULL`); всё — `kind <> 'event'`, `hidden = 0`,
  `deleted = 0`; тексты (D5) — ещё `purged = 0`.
- D3 скорость, D4 частота, D5 как владелец пишет (только тексты, которые писал сам владелец), D6 реакции (без реакций
  сессий, ключ — `reactions._key`, П7), D7 «знакомый».

Строки ответа — ПДн (цифры переписки с людьми): в журнал пишутся только число строк и время прохода.
"""

import json
import logging
import re
import statistics
import threading
import time
from collections import defaultdict

import emoji as emoji_lib

from .db import Db
from .reactions import _key as emoji_key

_logger = logging.getLogger(__name__)

SCHEMAS = ("main", "work")
DAY_MS = 24 * 60 * 60 * 1000
WINDOW_MS = 365 * DAY_MS
D30_MS = 30 * DAY_MS
UNANSWERED_MS = 7 * DAY_MS  # D3: ответ позже 7 суток или нет ответа дольше — «без ответа»
SAMPLES_MAX = 100  # D3: последних задержек каждой стороны
LENS_MAX = 200  # D5: длин последних текстов
EXAMPLES_MAX = 3  # D5: образцов
EXAMPLE_MIN_LEN = 10
CHANNELS_MAX = 20  # D4: бесед в топе
REACT_TOP = 8  # D6
SLOW_PASS_S = 30.0  # D: проход дольше — предупреждение в журнал
TEXT_READ_MAX = 2000  # D: из текста владельца читается не больше

_TY = frozenset("ты тебя тебе тобой твой твоя твоё твое твои твоего твоей твоих твоему твоим".split())
_VY = frozenset("вы вас вам вами ваш ваша ваше ваши вашего вашей ваших вашему вашим".split())
_WORD_RE = re.compile(r"\w+")
_GREET_RE = re.compile(
    r"^(?:привет|здравствуй(?:те)?|добрый\s+(?:день|вечер|утро)|доброе\s+утро|доброй\s+ночи|хай|hi|hello|hey)"
    r"(?!\w)",
    re.IGNORECASE,
)
_SLACK_EMOJI_RE = re.compile(r":[a-z0-9_+'-]+:")
_CYR_RE = re.compile(r"[Ѐ-ӿ]")
_LAT_RE = re.compile(r"[A-Za-z]")


def _sql(schema: str, template: str, **parts: str) -> str:
    """Тот же приём, что `queries._sql`: текст запроса — литерал в вызове, подставляются только доверенные
    фрагменты (имя схемы)."""
    return template.format(schema=schema, **parts)


# --- текст владельца (D5) --------------------------------------------------------------------------------------


def text_features(text: str) -> dict:
    """Признаки текста владельца: длина после `strip`, «ты»/«вы», эмодзи, приветствие, кириллица/латиница."""
    value = text.strip()
    words = {w.casefold() for w in _WORD_RE.findall(value)}
    cyr = len(_CYR_RE.findall(value))
    lat = len(_LAT_RE.findall(value))
    return {
        "len": len(value),
        "ty": bool(words & _TY),
        "vy": bool(words & _VY),
        "emoji": emoji_lib.emoji_count(value) > 0 or _SLACK_EMOJI_RE.search(value) is not None,
        "greet": _GREET_RE.match(value) is not None,
        "cyr": cyr > lat,
        "lat": lat > cyr,
    }


def _new_style() -> dict:
    return {"n": 0, "lens": [], "ty": 0, "vy": 0, "emoji": 0, "greet": 0, "cyr": 0, "lat": 0, "examples": []}


def _new_acc() -> dict:
    return {
        "in": {"total": 0, "d30": 0, "personal": 0, "first_ts": None, "last_ts": None},
        "out": {"total": 0, "d30": 0, "first_ts": None, "last_ts": None},
        "seen": {"total": 0, "last_ts": None},
        "reply_me": {"samples": [], "unanswered": 0},
        "reply_them": {"samples": [], "unanswered": 0},
        "style": {"chat": _new_style(), "mail": _new_style()},
        # ключ сравнения (`emoji_key`) -> [показываемое написание, n]
        "react": {"me_to_them": {}, "them_to_me": {}},
        "channels": {},  # channel_id -> [in, out, seen]
    }


def _bump_times(part: dict, ts: int) -> None:
    if "first_ts" in part and (part["first_ts"] is None or ts < part["first_ts"]):
        part["first_ts"] = ts
    if part["last_ts"] is None or ts > part["last_ts"]:
        part["last_ts"] = ts


def _react_add(bucket: dict, value: str) -> None:
    key = emoji_key(value)
    entry = bucket.get(key)
    if entry is None:
        bucket[key] = [value, 1]
    else:
        entry[1] += 1


def _reactions(raw: str | None) -> list[dict]:
    try:
        value = json.loads(raw) if raw else []
    except ValueError, TypeError:
        return []
    return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []


def _mail_to(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except ValueError, TypeError:
        return None
    if not isinstance(value, list):
        return None
    return [str(a).strip().lower() for a in value if isinstance(a, str) and a.strip()]


# --- проход по одному файлу -----------------------------------------------------------------------------


class _File:
    """Состояние прохода по одному файлу: контакты-люди, индексы адресов, накопители."""

    def __init__(self, conn, schema: str, now_ms: int) -> None:
        self.conn = conn
        self.schema = schema
        self.now_ms = now_ms
        self.since = now_ms - WINDOW_MS
        self.people: dict[int, tuple[int, str]] = {}  # contact_id -> (account_id, ext_id)
        self.by_email: dict[tuple[int, str], list[int]] = defaultdict(list)
        for cid, account_id, ext_id, email in conn.execute(
            _sql(
                schema,
                "SELECT id, account_id, ext_id, email FROM {schema}.contact WHERE person_id IS NOT NULL",
            )
        ):
            self.people[cid] = (account_id, ext_id)
            if email:
                self.by_email[(account_id, email.lower())].append(cid)
        self.session_marks = {
            (mid, key)
            for mid, key in conn.execute(
                _sql(schema, "SELECT message_id, emoji_key FROM {schema}.reaction_by_session")
            )
        }
        self.acc: dict[int, dict] = defaultdict(_new_acc)

    # --- D1–D6 по беседе -----------------------------------------------------------------------------

    def messages(self, ch: tuple) -> tuple[list[dict], bool, bool]:
        """Сообщения беседы за окно в порядке ленты с проставленными `to_me` (чужое «Владельцу») и `targets`
        (контакты-люди, кому писал владелец) — D1 на всех загруженных сообщениях беседы. Возвращает `(msgs,
        personal, mail)`. Ф24-1 П3: этим же пользуется контекст фактов о людях (`person_facts.context`)."""
        channel_id, kind, ch_ext_id, account_id, slack_type, source = ch
        rows = self.conn.execute(
            _sql(
                self.schema,
                "SELECT m.id, m.ts, m.ext_id, m.thread_ext_id, m.reply_to_ext_id, m.sender_contact_id, "
                "m.is_me, m.kind, m.mentions_me, m.mentions_broadcast, m.mail_to_json, m.reactions_json, "
                "m.purged, "
                "CASE WHEN m.is_me = 1 THEN substr(m.text, 1, {text_max}) END, m.outbox_id, o.origin, "
                "o.draft_text IS NOT NULL "
                "FROM {schema}.message m LEFT JOIN {schema}.outbox o ON o.id = m.outbox_id "
                "WHERE m.channel_id = ? AND m.ts >= ? AND m.hidden = 0 AND m.deleted = 0 "
                "AND m.kind <> 'event' "
                # П1: заготовка outbox (черновик, график, неудача) — не сообщение владельца.
                "AND (m.is_me = 0 OR m.ext_id IS NOT NULL) "
                "ORDER BY m.ts, m.ext_sort, m.ext_id",
                text_max=str(TEXT_READ_MAX),
            ),
            (channel_id, self.since),
        ).fetchall()
        if not rows:
            return [], False, False
        msgs = [
            {
                "id": r[0],
                "ts": r[1],
                "ext_id": r[2],
                "thread": r[3],
                "reply_to": r[4],
                "sender": r[5],
                "me": bool(r[6]),
                "kind": r[7],
                "direct": bool(r[8]) and not r[9],
                "mail_to": r[10],
                "reactions": r[11],
                "purged": bool(r[12]),
                "text": r[13],
                "outbox_id": r[14],
                "origin": r[15],
                "edited": bool(r[16]),
            }
            for r in rows
        ]
        personal = kind == "dm" or slack_type == "mpim"
        mail = kind == "mail_thread"
        foreign = {m["sender"] for m in msgs if not m["me"] and m["sender"] is not None}
        self._address(msgs, kind, personal, mail, foreign, ch_ext_id, account_id, source)
        return msgs, personal, mail

    def channel(self, ch: tuple) -> None:
        channel_id, kind = ch[0], ch[1]
        msgs, personal, mail = self.messages(ch)
        if not msgs:
            return
        thread_keys = (
            set()
            if personal or mail
            else {m["thread"] for m in msgs if m["thread"] is not None and m["thread"] != m["ext_id"]}
        )
        self._lines(msgs, personal or mail, thread_keys)
        style_kind = "mail" if mail else "chat"
        for m in msgs:
            sender = m["sender"]
            if not m["me"] and sender in self.people:
                acc = self.acc[sender]
                acc["seen"]["total"] += 1
                _bump_times(acc["seen"], m["ts"])
                acc["channels"].setdefault(channel_id, [0, 0, 0])[2] += 1
                if m["to_me"]:
                    part = acc["in"]
                    part["total"] += 1
                    if m["ts"] >= self.now_ms - D30_MS:
                        part["d30"] += 1
                    if not mail:
                        part["personal"] += 1
                    _bump_times(part, m["ts"])
                    acc["channels"][channel_id][0] += 1
                # D6 владелец -> P: мои реакции на его сообщения, кроме поставленных сессией (П7: без U+FE0F).
                for r in _reactions(m["reactions"]):
                    value = r.get("emoji")
                    if r.get("me") is True and isinstance(value, str) and value:
                        if (m["id"], emoji_key(value)) in self.session_marks:
                            continue
                        _react_add(acc["react"]["me_to_them"], value)
            if not m["me"]:
                continue
            for target in m["targets"]:
                acc = self.acc[target]
                part = acc["out"]
                part["total"] += 1
                if m["ts"] >= self.now_ms - D30_MS:
                    part["d30"] += 1
                _bump_times(part, m["ts"])
                acc["channels"].setdefault(channel_id, [0, 0, 0])[1] += 1
                if kind == "dm":
                    # D6 P -> владельцу: в личке чужая реакция на моё сообщение — это P (П2: только `kind = 'dm'`).
                    for r in _reactions(m["reactions"]):
                        value = r.get("emoji")
                        count = r.get("count")
                        if (
                            r.get("me") is False
                            and isinstance(value, str)
                            and value
                            and isinstance(count, int)
                            and count > 0
                        ):
                            _react_add(acc["react"]["them_to_me"], value)
                self._style(acc["style"][style_kind], m)

    def _address(self, msgs, kind, personal, mail, foreign, ch_ext_id, account_id, source) -> None:
        """D1 (+П2): `to_me` у чужих сообщений людей, `targets` (контакты-люди) у сообщений владельца."""
        single = next(iter(foreign)) if len(foreign) == 1 else None
        if single is not None and single not in self.people:
            single = None
        if personal:
            dm_target = None
            if kind == "dm" and single is not None:
                if source != "telegram" or self.people[single][1] == ch_ext_id:
                    dm_target = single
            prev_foreign = None
            for m in msgs:
                if m["me"]:
                    target = dm_target if kind == "dm" else prev_foreign
                    m["targets"] = [target] if target is not None and target in self.people else []
                    m["to_me"] = False
                else:
                    m["to_me"] = m["sender"] in self.people
                    m["targets"] = []
                    if m["sender"] is not None:
                        prev_foreign = m["sender"]
            return
        if mail:
            for m in msgs:
                m["to_me"] = not m["me"] and m["sender"] in self.people
                m["targets"] = []
                if not m["me"]:
                    continue
                addresses = _mail_to(m["mail_to"])
                if addresses is None:
                    m["targets"] = [single] if single is not None else []
                    continue
                targets: list[int] = []
                for address in addresses:
                    for cid in self.by_email.get((account_id, address), ()):
                        if cid not in targets:
                            targets.append(cid)
                m["targets"] = targets
            return
        by_ext = {m["ext_id"]: m for m in msgs if m["ext_id"] is not None}
        threads: dict[str, dict] = {}
        for m in msgs:
            m["to_me"] = False
            m["targets"] = []
            ref = by_ext.get(m["reply_to"]) if m["reply_to"] is not None else None
            thread = m["thread"] if m["thread"] is not None and m["thread"] != m["ext_id"] else None
            state = None
            if thread is not None:
                state = threads.get(thread)
                if state is None:
                    parent = by_ext.get(thread)
                    state = threads[thread] = {
                        "parent_me": parent is not None and parent["me"],
                        "me_wrote": parent is not None and parent["me"],
                        "last_me": parent is not None and parent["me"],
                        "last_foreign": parent["sender"] if parent is not None and not parent["me"] else None,
                    }
            if m["me"]:
                if ref is not None and not ref["me"] and ref["sender"] in self.people:
                    m["targets"] = [ref["sender"]]
                elif state is not None and state["last_foreign"] in self.people:
                    m["targets"] = [state["last_foreign"]]
            elif m["sender"] in self.people:
                in_my_thread = state is not None and (
                    state["parent_me"] or (state["me_wrote"] and state["last_me"])
                )
                m["to_me"] = m["direct"] or (ref is not None and ref["me"]) or in_my_thread
            if state is not None:
                state["last_me"] = m["me"]
                if m["me"]:
                    state["me_wrote"] = True
                elif m["sender"] is not None:
                    state["last_foreign"] = m["sender"]

    def _lines(self, msgs, whole: bool, thread_keys: set) -> None:
        """D2 + D3: линия — беседа (личка, письма, прочее) или тред Slack (родитель — в линии своего треда);
        пачка P «Владельцу» -> первое следующее сообщение владельца; пачка владельца «P» -> следующее сообщение P."""
        lines: dict = defaultdict(list)
        for m in msgs:
            if whole:
                key = None
            elif m["thread"] is not None:
                key = m["thread"]
            elif m["ext_id"] in thread_keys:
                key = m["ext_id"]
            else:
                key = None
            lines[key].append(m)
        for line in lines.values():
            open_in: dict[int, int] = {}
            open_out: dict[int, int] = {}
            for m in line:
                if m["me"]:
                    for person, start in open_in.items():
                        self._sample("reply_me", person, start, m["ts"])
                    open_in.clear()
                    for person in m["targets"]:
                        open_out.setdefault(person, m["ts"])
                    continue
                person = m["sender"]
                if person not in self.people:
                    continue
                start = open_out.pop(person, None)
                if start is not None:
                    self._sample("reply_them", person, start, m["ts"])
                if m["to_me"]:
                    open_in.setdefault(person, m["ts"])
            for side, opened in (("reply_me", open_in), ("reply_them", open_out)):
                for person, start in opened.items():
                    if self.now_ms - start > UNANSWERED_MS:
                        self.acc[person][side]["unanswered"] += 1

    def _sample(self, side: str, person: int, start: int, end: int) -> None:
        delay = end - start
        if delay > UNANSWERED_MS:
            self.acc[person][side]["unanswered"] += 1
        else:
            self.acc[person][side]["samples"].append((start, max(delay, 0)))

    def _style(self, style: dict, m: dict) -> None:
        """D5: только тексты владельца, которые писал он сам (не черновик сессии, ушедший без правки)."""
        if m["kind"] != "text" or m["purged"] or not m["text"] or not m["text"].strip():
            return
        if not (m["outbox_id"] is None or m["origin"] == "ui" or m["edited"]):
            return
        f = text_features(m["text"])
        style["n"] += 1
        style["lens"].append((m["ts"], f["len"]))
        for name in ("ty", "vy", "emoji", "greet", "cyr", "lat"):
            if f[name]:
                style[name] += 1
        if f["len"] >= EXAMPLE_MIN_LEN:
            style["examples"].append((m["ts"], m["id"]))

    # --- D6: реакции писем P -> владельцу -------------------------------------------------------------------

    def mail_reactions(self) -> None:
        rows = self.conn.execute(
            _sql(
                self.schema,
                "SELECT r.emoji, r.sender_key, ch.account_id FROM {schema}.mail_reaction r "
                "JOIN {schema}.channel ch ON ch.id = r.channel_id "
                "JOIN {schema}.message m ON m.channel_id = r.channel_id AND m.ext_id = r.target_ext_id "
                "WHERE r.is_me = 0 AND r.emoji <> '' AND m.is_me = 1 AND m.hidden = 0 AND m.deleted = 0 "
                "AND m.ts >= ?",
            ),
            (self.since,),
        ).fetchall()
        for value, sender_key, account_id in rows:
            if not isinstance(sender_key, str):
                continue
            for cid in self.by_email.get((account_id, sender_key.strip().lower()), ()):
                _react_add(self.acc[cid]["react"]["them_to_me"], value)

    # --- итог -----------------------------------------------------------------------------------------

    def rows(self) -> list[tuple]:
        out = []
        for cid, acc in self.acc.items():
            if not (acc["seen"]["total"] or acc["out"]["total"] or acc["react"]["them_to_me"]):
                continue
            stats = finish(acc)
            known = 1 if stats["out"]["total"] >= 1 or stats["in"]["personal"] >= 1 else 0
            times = [t for t in (stats["in"]["last_ts"], stats["out"]["last_ts"]) if t is not None]
            out.append(
                (
                    cid,
                    known,
                    max(times) if times else None,
                    stats["seen"]["last_ts"],
                    stats["in"]["total"],
                    stats["out"]["total"],
                    stats["in"]["d30"],
                    stats["out"]["d30"],
                    json.dumps(stats, ensure_ascii=False, separators=(",", ":")),
                )
            )
        return out


def finish(acc: dict) -> dict:
    """Накопитель контакта -> `stats_json` D8 (`v: 1`)."""

    def _last(samples: list[tuple[int, int]], limit: int) -> list[int]:
        return [value for _ts, value in sorted(samples)[-limit:]]

    style = {}
    for name, s in acc["style"].items():
        style[name] = {
            "n": s["n"],
            "lens": _last(s["lens"], LENS_MAX),
            "ty": s["ty"],
            "vy": s["vy"],
            "emoji": s["emoji"],
            "greet": s["greet"],
            "cyr": s["cyr"],
            "lat": s["lat"],
            "examples": _last(s["examples"], EXAMPLES_MAX),
        }
    react = {}
    for side, bucket in acc["react"].items():
        top = sorted(bucket.values(), key=lambda e: -e[1])[:REACT_TOP]
        react[side] = {value: n for value, n in top}
    channels = sorted(
        ([cid, *counts] for cid, counts in acc["channels"].items()),
        key=lambda c: (-(c[1] + c[2]), -c[3], c[0]),
    )[:CHANNELS_MAX]
    return {
        "v": 1,
        "in": dict(acc["in"]),
        "out": dict(acc["out"]),
        "seen": dict(acc["seen"]),
        "reply_me": {
            "samples": _last(acc["reply_me"]["samples"], SAMPLES_MAX),
            "unanswered": acc["reply_me"]["unanswered"],
        },
        "reply_them": {
            "samples": _last(acc["reply_them"]["samples"], SAMPLES_MAX),
            "unanswered": acc["reply_them"]["unanswered"],
        },
        "style": style,
        "react": react,
        "channels": channels,
    }


# --- проход целиком --------------------------------------------------------------------------------------


def compute(conn, now_ms: int, stop: threading.Event | None = None) -> dict[str, list[tuple]] | None:
    """Строки `contact_stats` по файлам (без `computed_ts`); `None` — пришёл сигнал остановки (П10)."""
    result: dict[str, list[tuple]] = {}
    for schema in SCHEMAS:
        state = _File(conn, schema, now_ms)
        channels = conn.execute(
            _sql(
                schema,
                "SELECT ch.id, ch.kind, ch.ext_id, ch.account_id, "
                "json_extract(ch.meta_json, '$.attrs.slack_type'), a.source "
                "FROM {schema}.channel ch LEFT JOIN main.account a ON a.id = ch.account_id "
                "ORDER BY ch.id",
            )
        ).fetchall()
        for ch in channels:
            if stop is not None and stop.is_set():
                return None
            state.channel(ch)
        state.mail_reactions()
        result[schema] = state.rows()
    if stop is not None and stop.is_set():
        return None
    return result


def write(conn, rows: dict[str, list[tuple]], now_ms: int) -> int:
    """Одна транзакция писателя: в каждом файле — всё заново."""
    written = 0
    for schema in SCHEMAS:
        conn.execute(_sql(schema, "DELETE FROM {schema}.contact_stats"))
        conn.executemany(
            _sql(
                schema,
                "INSERT INTO {schema}.contact_stats (contact_id, known, last_ts, seen_ts, in_total, "
                "out_total, in_30d, out_30d, stats_json, computed_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ),
            [(*row, now_ms) for row in rows.get(schema, ())],
        )
        written += len(rows.get(schema, ()))
    return written


def recompute(db: Db, now_ms: int, stop: threading.Event | None = None) -> int | None:
    """Часовой пересчёт: чтение своим `db.reader()` (вызывать в потоке), запись — одной транзакцией. Число
    строк или `None` — остановка ядра посреди прохода (ничего не записано)."""
    started = time.monotonic()
    with db.reader() as conn:
        rows = compute(conn, now_ms, stop)
    if rows is None or (stop is not None and stop.is_set()):
        _logger.info("люди: пересчёт статистики прерван остановкой")
        return None
    written = db.write_sync(lambda conn: write(conn, rows, now_ms))
    elapsed = time.monotonic() - started
    if elapsed > SLOW_PASS_S:
        _logger.warning("люди: пересчёт статистики шёл %.1f с (строк %s)", elapsed, written)
    else:
        _logger.info("люди: пересчёт статистики за %.1f с (строк %s)", elapsed, written)
    return written


# --- чтение: слияние контактов человека и вывод -----------------------------------------------------------


def load(raw: str | None) -> dict | None:
    try:
        value = json.loads(raw) if raw else None
    except ValueError, TypeError:
        return None
    return value if isinstance(value, dict) and value.get("v") == 1 else None


def _min(values):
    present = [v for v in values if v is not None]
    return min(present) if present else None


def _max(values):
    present = [v for v in values if v is not None]
    return max(present) if present else None


def merge(items: list[dict]) -> dict:
    """D8: человек из нескольких контактов — суммы, min/max времён, объединение выборок и счётчиков, беседы —
    объединение (одна беседа у двух контактов — суммой)."""

    def _part(name: str, *, first: bool) -> dict:
        parts = [i.get(name) or {} for i in items]
        out = {key: sum(int(p.get(key) or 0) for p in parts) for key in ("total", "d30", "personal")}
        if name != "in":
            out.pop("personal")
        if name == "seen":
            out.pop("d30")
        if first:
            out["first_ts"] = _min(p.get("first_ts") for p in parts)
        out["last_ts"] = _max(p.get("last_ts") for p in parts)
        return out

    def _reply(name: str) -> dict:
        parts = [i.get(name) or {} for i in items]
        samples: list[int] = []
        for p in parts:
            samples.extend(int(v) for v in p.get("samples") or [] if isinstance(v, int))
        return {"samples": samples, "unanswered": sum(int(p.get("unanswered") or 0) for p in parts)}

    style = {}
    for kind in ("chat", "mail"):
        parts = [(i.get("style") or {}).get(kind) or {} for i in items]
        counters = ("n", "ty", "vy", "emoji", "greet", "cyr", "lat")
        merged = {key: sum(int(p.get(key) or 0) for p in parts) for key in counters}
        merged["lens"] = [v for p in parts for v in p.get("lens") or [] if isinstance(v, int)]
        merged["examples"] = [v for p in parts for v in p.get("examples") or [] if isinstance(v, int)]
        style[kind] = merged
    react: dict[str, dict] = {}
    for side in ("me_to_them", "them_to_me"):
        bucket: dict[str, list] = {}
        for i in items:
            for value, n in ((i.get("react") or {}).get(side) or {}).items():
                key = emoji_key(value)
                entry = bucket.setdefault(key, [value, 0])
                entry[1] += int(n or 0)
        react[side] = bucket
    channels: dict[int, list[int]] = {}
    for i in items:
        for entry in i.get("channels") or []:
            if not isinstance(entry, list) or len(entry) != 4:
                continue
            cid, n_in, n_out, n_seen = entry
            acc = channels.setdefault(cid, [0, 0, 0])
            acc[0] += n_in
            acc[1] += n_out
            acc[2] += n_seen
    return {
        "in": _part("in", first=True),
        "out": _part("out", first=True),
        "seen": _part("seen", first=False),
        "reply_me": _reply("reply_me"),
        "reply_them": _reply("reply_them"),
        "style": style,
        "react": react,
        "channels": sorted(
            ([cid, *counts] for cid, counts in channels.items()),
            key=lambda c: (-(c[1] + c[2]), -c[3], c[0]),
        ),
    }


def quantile(values: list[int], q: float) -> int:
    """Квантиль с линейной интерполяцией; пустая выборка — 0."""
    if not values:
        return 0
    ordered = sorted(values)
    pos = q * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (pos - low))


def _reply_summary(part: dict) -> dict:
    samples = part["samples"]
    return {
        "median_ms": quantile(samples, 0.5),
        "p75_ms": quantile(samples, 0.75),
        "n": len(samples),
        "unanswered": part["unanswered"],
    }


def _address(ty: int, vy: int) -> str | None:
    if ty > 0 and ty >= 2 * vy:
        return "ты"
    if vy > 0 and vy >= 2 * ty:
        return "вы"
    if ty > 0 and vy > 0:
        return "смешанно"
    return None


def _lang(cyr: int, lat: int) -> str | None:
    total = cyr + lat
    if not total:
        return None
    share = cyr / total
    if share >= 0.8:
        return "ru"
    if share <= 0.2:
        return "en"
    return "mixed"


def _style_summary(style: dict) -> dict | None:
    n = style["n"]
    if not n:
        return None
    return {
        "n": n,
        "median_len": round(statistics.median(style["lens"])) if style["lens"] else 0,
        "address": _address(style["ty"], style["vy"]),
        "emoji_share": round(style["emoji"] / n, 2),
        "greet_share": round(style["greet"] / n, 2),
        "lang": _lang(style["cyr"], style["lat"]),
    }


def summary(merged: dict) -> dict:
    """Ответ карточки E2 (`stats`) из слитой статистики."""

    def _top(bucket: dict) -> list[dict]:
        entries = sorted(bucket.values(), key=lambda e: -e[1])[:REACT_TOP]
        return [{"key": value, "n": n} for value, n in entries]

    return {
        "in": merged["in"],
        "out": merged["out"],
        "seen": merged["seen"],
        "reply_me": _reply_summary(merged["reply_me"]),
        "reply_them": _reply_summary(merged["reply_them"]),
        "style": {kind: _style_summary(s) for kind, s in merged["style"].items()},
        "react": {side: _top(bucket) for side, bucket in merged["react"].items()},
    }
