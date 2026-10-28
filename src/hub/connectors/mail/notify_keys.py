"""Ф84-1: письма-уведомления с домена склеиваются в беседу по ключу (спека phase84-01).

Правила — `account.config_json["notify_keys"]` (до 20 объектов). Письмо с домена правила, в котором шаблон
находит ключ, -> беседа `notify:<name>:<key>` вида «Задачи» (образец — трекеры Jira/Trello, `trackers.py`).
**Текст не теряется никогда:** ключ не найден, тело частичное или не разобралось -> `route` даёт `None` и
письмо идёт прежним путём; после обрезки по `cut` пусто -> общая чистка -> исходный текст.

Чистые функции, без сети; в журнал — только номер/имя правила и причина, без текста писем.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Collection
from typing import Any

from .. import refs as refs_mod
from . import convert, textproc
from .imapwire import FetchedMsg
from .trackers import TrackerPart, clean_generic

_logger = logging.getLogger("hub.mail")

TRACKER = "notify"
PREFIX = "notify:"
MAX_RULES = 20
MAX_TITLE_LEN = 200
MAX_PREFIX_LEN = 20
MAX_CUTS = 10
MAX_CUT_LEN = 80
SEARCH_LIMIT = 64 * 1024  # поиск — по первым 64 КБ текста
_NAME_RE = re.compile(r"[a-z0-9-]{1,20}")
_KEY_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_KEY_BAD_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_DOMAIN_RE = re.compile(r"(?:[a-z0-9-]+\.)+[a-z0-9-]+")
_WHERE = ("subject", "text", "both")
_TITLE_URL_TAIL_RE = re.compile(r"\s*\(https?://[^)]*\)?\s*$")


class RuleError(ValueError):
    pass


def _rule(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise RuleError("правило — не объект")
    name = raw.get("name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise RuleError("name")
    domain = raw.get("from")
    if not isinstance(domain, str) or not _DOMAIN_RE.fullmatch(domain.strip().lower()):
        raise RuleError("from")
    where = raw.get("where", "text")
    if where not in _WHERE:
        raise RuleError("where")
    try:
        key_re = re.compile(str(raw.get("key")))
        title_raw = raw.get("title")
        title_re = re.compile(title_raw) if title_raw is not None else None
    except re.error, TypeError:
        raise RuleError("regex") from None
    if key_re.groups < 1 or (title_re is not None and title_re.groups < 1):
        raise RuleError("группа 1")
    prefix = raw.get("title_prefix", "")
    if not isinstance(prefix, str) or len(prefix) > MAX_PREFIX_LEN:
        raise RuleError("title_prefix")
    url = raw.get("url")
    if url is not None and not (isinstance(url, str) and url.startswith("https://") and "{key}" in url):
        raise RuleError("url")
    cut = raw.get("cut", [])
    if (
        not isinstance(cut, list)
        or len(cut) > MAX_CUTS
        or not all(isinstance(c, str) and 0 < len(c) <= MAX_CUT_LEN for c in cut)
    ):
        raise RuleError("cut")
    return {
        "name": name,
        "from": domain.strip().lower(),
        "key_re": key_re,
        "where": where,
        "title_re": title_re,
        "title_prefix": prefix,
        "url": url,
        "cut": list(cut),
    }


def parse_rules(value: Any) -> list[dict]:
    """`notify_keys` конфига -> годные правила. Негодное правило пропускается с одной строкой WARNING."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            _logger.warning("mail: notify_keys — не JSON, правила пропущены")
            return []
    if not isinstance(value, list):
        _logger.warning("mail: notify_keys — не список, правила пропущены")
        return []
    if len(value) > MAX_RULES:
        _logger.warning("mail: notify_keys — больше %d правил, лишние пропущены", MAX_RULES)
    rules: list[dict] = []
    seen: set[str] = set()
    for i, raw in enumerate(value[:MAX_RULES]):
        try:
            rule = _rule(raw)
            if rule["name"] in seen:
                raise RuleError("name повторяется")
        except RuleError as exc:
            _logger.warning("mail: notify_keys[%d] пропущено (%s)", i, exc)
            continue
        seen.add(rule["name"])
        rules.append(rule)
    return rules


def fingerprint(rules: list[dict]) -> str:
    """Отпечаток правил для признака «перенос старых писем пройден»; нет правил — пустая строка."""
    if not rules:
        return ""
    parts = [
        [
            r["name"],
            r["from"],
            r["key_re"].pattern,
            r["where"],
            r["title_re"].pattern if r["title_re"] else None,
            r["title_prefix"],
            r["url"],
            r["cut"],
        ]
        for r in rules
    ]
    return hashlib.sha1(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:16]  # noqa: S324


def is_notify_channel(ext_id: str | None) -> bool:
    return bool(ext_id) and str(ext_id).startswith(PREFIX)


def _domain_matches(domain: str, rule_domain: str) -> bool:
    d = (domain or "").lower()
    return d == rule_domain or d.endswith("." + rule_domain)


def rule_of_address(address: str | None, rules: list[dict]) -> dict | None:
    domain = convert.domain_of(address or "")
    return next((r for r in rules if _domain_matches(domain, r["from"])), None)


def _first_group(pattern: re.Pattern, text: str) -> str | None:
    m = pattern.search(text[:SEARCH_LIMIT])
    if not m or m.group(1) is None:
        return None
    return m.group(1)


def _norm_key(raw: str) -> str | None:
    """Ключ как есть, если годен; иначе — в нижнем регистре, чужие знаки (пробел, `@`) -> `_` (ключом бывает
    адрес почты или имя). Кириллица и прочее, что так не сводится, — `None`."""
    if _KEY_RE.fullmatch(raw):
        return raw
    key = _KEY_BAD_RE.sub("_", raw.strip().lower()).strip("_")[:64]
    return key if _KEY_RE.fullmatch(key) and len(key) * 2 >= len(raw.strip()) else None


def _find_key(rule: dict, subject: str, text: str) -> str | None:
    sources = {"subject": [subject], "text": [text], "both": [subject, text]}[rule["where"]]
    for src in sources:
        raw = _first_group(rule["key_re"], src)
        key = _norm_key(raw) if raw is not None else None
        if key is not None:
            return key
    return None


def _find_title(rule: dict, subject: str, text: str) -> str | None:
    if rule["title_re"] is None:
        return None
    for src in (text, subject):
        raw = _first_group(rule["title_re"], src)
        if raw is None:
            continue
        raw = _TITLE_URL_TAIL_RE.sub("", " ".join(raw.split()))
        raw = raw[:MAX_TITLE_LEN].strip()
        if raw:
            return raw
    return None


def cut_text(text: str, markers: list[str]) -> str:
    """Обрезать с первого вхождения любого из маркеров (письмо из HTML бывает одной строкой)."""
    cuts = [i for i in (text.find(mk) for mk in markers) if i >= 0]
    return text[: min(cuts)].strip() if cuts else text


def route(
    msg: FetchedMsg,
    rules: list[dict],
    *,
    thread_msgs: list[FetchedMsg],
    known: Collection[str] = (),
) -> list[TrackerPart] | None:
    """Письмо с домена правила, где найден ключ -> одна часть (беседа `notify:<name>:<key>`); иначе `None`
    (обычное письмо). Письмо, которое берут трекеры Jira/Trello, правилами не проверяется — это решает
    вызывающий (`trackers.route` первым)."""
    if not rules:
        return None
    if msg.body_partial:
        return None  # частичное тело — текст неполный: обычное письмо
    raw = msg.body or b""
    from_list = convert.addresses(raw, "From")
    if not from_list:
        return None
    rule = rule_of_address(from_list[0][1], rules)
    if rule is None:
        return None
    body = textproc.extract_body(raw, partial=msg.body_partial, strip_quote=False)
    if body.unparsed or not body.text:
        return None
    subject = convert.decoded_header(raw, "Subject") or ""
    key = _find_key(rule, subject, body.text)
    if key is None:
        return None
    ext_id = f"{PREFIX}{rule['name']}:{key}"
    name = _find_title(rule, subject, body.text)
    prefix = rule["title_prefix"]
    if name:
        title: str | None = f"{prefix}{name}"
    else:
        title = f"{prefix}{rule['name']} {key}" if ext_id not in known else None
    cleaned = clean_generic(body.text)
    text_out = cut_text(cleaned, rule["cut"]) or cleaned or body.text
    url = rule["url"].format(key=key) if rule["url"] else None
    extra: dict = {"tracker": TRACKER, "key": key, "no_reply": True}
    if url:
        extra["url"] = url
    channel: dict = {"ext_id": ext_id, "kind": "task", "own_id_space": True}
    if title is not None:
        channel["title"] = title
    channel["attrs"] = convert.thread_attrs(thread_msgs, extra=extra)
    refs = [url] if url else []
    for r in refs_mod.extract_refs(text_out):
        if r not in refs:
            refs.append(r)
    return [
        TrackerPart(
            channel=channel,
            tracker=TRACKER,
            key=key,
            url=url or "",
            actor=None,
            text=text_out,
            mentions_me=False,
            refs=refs[: refs_mod.MAX_REFS],
            attachments=list(body.attachments),
        )
    ]
