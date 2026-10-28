"""Правило почты по домену регистрации (Ф95-1, docs/architecture.md).

Сессия зовёт владельца заявкой «нужен человек» на площадке (вход, регистрация, код) — письма площадки должны лечь в
проект заявки, а не туда, куда ящик раскладывает всё остальное (правило «весь аккаунт»). При создании заявки
ядро заводит на каждом почтовом аккаунте правило `from_domains = <домен регистрации>` -> проект заявки
(`map_rule.origin_human_id = <id заявки>`), если у аккаунта такого условия ещё нет ни в какой проект. Итог
`done`/`later` — правило остаётся; `failed`/`expired`/`cancelled` — снимается (`drop_for_request`).

Домен — последние две метки хоста (для двухуровневых зон — три): почтовый коннектор кладёт в `from_domains`
ТОЧНЫЙ домен адреса From (`msgid.domain_of`), совпадение у правила тоже точное — письма с поддомена
(`noreply@mail.<домен>`) правило не поймает (отступление спеки, A.6).

Вызывается ТОЛЬКО внутри `Db.write()` — в той же транзакции, что заявка или её итог."""

import ipaddress
import json
import re
import sqlite3
from urllib.parse import urlsplit

from . import mapping
from .audit import write_audit

# A.2: двухуровневые зоны — домен регистрации из трёх меток.
TWO_LEVEL_ZONES = frozenset(
    {
        "com.ru",
        "net.ru",
        "org.ru",
        "pp.ru",
        "spb.ru",
        "msk.ru",
        "co.uk",
        "org.uk",
        "ac.uk",
        "com.ua",
        "in.ua",
        "org.ua",
        "net.ua",
        "kiev.ua",
        "com.br",
        "com.au",
        "co.jp",
        "co.il",
        "com.tr",
        "com.cn",
        "co.kz",
        "com.kz",
    }
)

# A.3: почтовики, соцсети и крупные сервисы — их письма идут по многим делам, правило не заводится.
BLOCKED_DOMAINS = frozenset(
    {
        "google.com",
        "gmail.com",
        "mail.ru",
        "bk.ru",
        "inbox.ru",
        "list.ru",
        "internet.ru",
        "yandex.ru",
        "ya.ru",
        "rambler.ru",
        "vk.com",
        "vk.ru",
        "ok.ru",
        "gosuslugi.ru",
        "apple.com",
        "icloud.com",
        "microsoft.com",
        "live.com",
        "outlook.com",
        "yahoo.com",
        "proton.me",
        "telegram.org",
        "t.me",
        "whatsapp.com",
        "facebook.com",
        "instagram.com",
        "github.com",
        "sberbank.ru",
        "tbank.ru",
        "tinkoff.ru",
        "ozon.ru",
        "wildberries.ru",
    }
)

DROP_STATUSES = ("failed", "expired", "cancelled")

_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
_SITE_RE = re.compile(r"[\w.-]+")


def _host_domain(host: str | None) -> str | None:
    """Хост -> домен регистрации или `None` (IP, `localhost`, одна метка, мусор)."""
    host = (host or "").strip().rstrip(".").lower()
    if not host:
        return None
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        return None
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return None
    if host.startswith("www."):
        host = host[4:]
    labels = host.split(".")
    if len(labels) < 2 or not all(_LABEL_RE.fullmatch(label) for label in labels):
        return None
    if not any(ch.isalpha() for ch in labels[-1]):
        return None  # зона без букв — не домен (`1.2.3.999`)
    take = 3 if ".".join(labels[-2:]) in TWO_LEVEL_ZONES else 2
    if len(labels) < take:
        return None  # сама зона (`com.ru`) — домена нет
    return ".".join(labels[-take:])


def request_domain(url: str | None, site: str | None) -> str | None:
    """A.2: домен регистрации заявки — из хоста `url`, а если `url` пуст — из `site`, когда он похож на домен
    (буквы, цифры, дефис, точки, есть точка)."""
    url = (url or "").strip()
    if url:
        try:
            parts = urlsplit(url if "://" in url else f"//{url}")
            host = parts.hostname
        except ValueError:
            return None
        return _host_domain(host)
    site = (site or "").strip()
    if not site or "." not in site or not _SITE_RE.fullmatch(site) or "_" in site:
        return None
    return _host_domain(site)


def _has_domain_rule(conn: sqlite3.Connection, account_id: int, domain: str) -> bool:
    """У аккаунта уже есть правило `from_domains = domain` (в любой проект)."""
    for (match_json,) in conn.execute(
        "SELECT match_json FROM main.map_rule WHERE account_id = ?", (account_id,)
    ):
        try:
            match = json.loads(match_json)
        except ValueError:
            continue
        if isinstance(match, dict) and match.get("attr") == "from_domains" and match.get("has") == domain:
            return True
    return False


def add_for_request(conn: sqlite3.Connection, req, *, now_ms: int) -> dict | None:
    """A.4: новая заявка `req` (`human_requests.Human`) — правила `from_domains = <домен>` -> проект заявки на
    почтовых аккаунтах, где такого условия ещё нет. Возвращает `{domain, project_id, project_name, rules:
    [{rule_id, account_id}]}`, если завелось хоть одно правило, иначе `None`."""
    if req.project_id is None:
        return None
    domain = request_domain(req.url, req.site)
    if domain is None or domain in BLOCKED_DOMAINS:
        return None
    accounts = [r[0] for r in conn.execute("SELECT id FROM main.account WHERE source = 'mail' ORDER BY id")]
    made: list[dict] = []
    for account_id in accounts:
        if _has_domain_rule(conn, account_id, domain):
            continue
        match = {
            "attr": "from_domains",
            "has": domain,
            "label": f"отправитель @{domain} (регистрация, заявка #{req.id})",
        }
        rule_id, moved = mapping.add_rule(
            conn, account_id=account_id, project_id=req.project_id, match=match, priority=None, now_ms=now_ms
        )
        conn.execute("UPDATE main.map_rule SET origin_human_id = ? WHERE id = ?", (req.id, rule_id))
        write_audit(
            conn,
            now_ms=now_ms,
            actor="core",
            api_token_id=None,
            client_name=None,
            request_id=f"human-{req.id}",
            action="mail_rule_auto",
            result="ok",
            detail={
                "human_id": req.id,
                "domain": domain,
                "account_id": account_id,
                "project_id": req.project_id,
                "rule_id": rule_id,
                "channels_relaid": moved,
            },
        )
        made.append({"rule_id": rule_id, "account_id": account_id})
    if not made:
        return None
    row = conn.execute("SELECT name FROM main.project WHERE id = ?", (req.project_id,)).fetchone()
    return {
        "domain": domain,
        "project_id": req.project_id,
        "project_name": row[0] if row is not None else None,
        "rules": made,
    }


def drop_for_request(conn: sqlite3.Connection, human_id: int, *, now_ms: int, status: str) -> int:
    """A.5: заявка закрылась провалом (`failed`, `expired`, `cancelled`) — снять заведённые ею правила с
    пересчётом бесед. Возвращает число снятых правил."""
    rows = conn.execute(
        "SELECT id, account_id, match_json, project_id FROM main.map_rule WHERE origin_human_id = ? "
        "ORDER BY id",
        (human_id,),
    ).fetchall()
    for rule_id, account_id, match_json, project_id in rows:
        _account_id, moved = mapping.remove_rule(conn, rule_id, now_ms)
        try:
            domain = json.loads(match_json).get("has")
        except ValueError, AttributeError:
            domain = None
        write_audit(
            conn,
            now_ms=now_ms,
            actor="core",
            api_token_id=None,
            client_name=None,
            request_id=f"human-{human_id}",
            action="mail_rule_auto_removed",
            result=status,
            detail={
                "human_id": human_id,
                "domain": domain,
                "account_id": account_id,
                "project_id": project_id,
                "rule_id": rule_id,
                "channels_relaid": moved,
            },
        )
    return len(rows)
