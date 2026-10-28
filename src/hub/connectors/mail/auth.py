"""`hub auth mail --address A [--title T] [--db main|work] [--account N] [--proxy socks5://…]` — вход в Gmail
по паролю приложения. Спецификация: `docs/architecture.md`

Запускает ТОЛЬКО владелец: пароль приложения вводится через getpass, проверяется (IMAP LOGIN + CAPABILITY с
`X-GM-EXT-1`/`IDLE` + LIST с `\\All`/`\\Sent` + SMTP EHLO/AUTH без отправки) и только после успешной проверки
пишется: аккаунт (`account.source='mail'`, конфиг без секретов) через админский сокет и файл секрета
`connectors/<id>.mail.json` (0600, атомарно). Ошибка -> ничего не записано, понятная причина. Пароль не
печатается, не логируется и не попадает в исключения/конфиг.

Ф12-4 (`docs/architecture.md`): `--imap host:port --smtp host:port [--name "Имя"]` — обычный
ящик (вид `imap`, пароль ящика): проверка IMAP LOGIN + CAPABILITY + LIST (найдены «Отправленные») + EXAMINE
INBOX + SMTP EHLO/AUTH без отправки; конфиг `flavor: imap`, хосты, `display_name` = `--name`,
`read_ack=false`.
Прокси — только явно (`--proxy none` или `socks5://…`, план 1.4). Тексты ошибок — без Gmail.
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from ...core import connector_auth
from .convert import DEFAULT_EXCLUDE_CATEGORIES
from .gateway import (
    FLAVOR_GMAIL,
    FLAVOR_IMAP,
    GmailGateway,
    MailAuthError,
    MailConfigError,
    MailError,
    MailUnavailable,
    ServerInfo,
    normalize_proxy,
)

_CONNECTORS_SUBDIR = "connectors"
CREDS_SUFFIX = ".mail.json"
_HEARTBEAT_FRESH_MS = 3 * 60 * 1000  # тот же порог, что у `hub auth slack`/`tg`

_AUTH_REASONS = {
    "bad_password": (
        "Gmail не принял адрес или пароль приложения. Пароль приложения создаётся на "
        "https://myaccount.google.com/apppasswords (нужна двухэтапная аутентификация); обычный пароль "
        "аккаунта не подходит."
    ),
    "imap_disabled": (
        "в настройках Gmail выключен IMAP: Настройки -> «Пересылка и POP/IMAP» -> «Включить IMAP» "
        "(для Workspace — может понадобиться администратор)."
    ),
    "web_login": (
        "Gmail просит сначала войти через браузер (проверка безопасности) — войдите в веб и повторите."
    ),
    "other": "Gmail отказал во входе — проверьте адрес, пароль приложения и включён ли IMAP.",
}
# Ф12-4: обычный ящик — без Gmail и «пароля приложения»
_PLAIN_AUTH_REASONS = {
    "bad_password": "почтовый сервер не принял адрес или пароль ящика.",
    "imap_disabled": "почтовый сервер сообщает, что IMAP для ящика выключен — включите его в настройках.",
    "web_login": "почтовый сервер просит сначала войти через веб-почту — войдите и повторите.",
    "other": "почтовый сервер отказал во входе — проверьте адрес и пароль ящика.",
}
_PLAIN_DEFAULT_PORTS = {"imap": 993, "smtp": 465}


def _hostport(value: str | None, what: str) -> tuple[str, int] | None:
    """`host[:port]` -> (host, port); порт по умолчанию — SSL (993/465). Кривое значение -> `ValueError`."""
    if value is None:
        return None
    text = value.strip()
    host, sep, port_s = text.rpartition(":")
    if not sep:
        host, port_s = text, ""
    host = host.strip().lower()
    if not host or " " in host or "/" in host:
        raise ValueError(f"--{what}: нужен адрес вида host:port")
    if not port_s:
        return host, _PLAIN_DEFAULT_PORTS[what]
    if not port_s.isdigit() or not 0 < int(port_s) < 65536:  # noqa: PLR2004
        raise ValueError(f"--{what}: порт — число 1…65535")
    return host, int(port_s)


def _write_json_atomic(path: Path, payload: dict) -> None:
    """0600, атомарно: уникальный временный файл в том же каталоге + `os.replace` (как у Slack/Telegram)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        os.chmod(tmp_path, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


async def _verify(
    gateway_factory: Callable[..., GmailGateway],
    *,
    address: str,
    password: str,
    proxy: str | None,
    check_smtp: bool = True,
    plain: dict | None = None,
) -> ServerInfo:
    """`plain` (Ф12-4) — вид `imap` и хосты; `None` — Gmail (фабрика зовётся как раньше, без лишних ключей).
    У вида `imap` «Отправленные» и `EXAMINE INBOX` проверяет само подключение шлюза."""
    extra = {} if plain is None else dict(plain)
    gw = gateway_factory(address=address, password=password, proxy=proxy, **extra)
    try:
        info = await gw.work.connect()
        if info.sent_mailbox is None:
            raise MailConfigError("нет папки \\Sent («Отправленные») в IMAP")
        if check_smtp:
            await gw.check_smtp_login()
        return info
    finally:
        with contextlib.suppress(Exception):
            await gw.close()


def _accounts(admin_client: httpx.Client) -> list[dict]:
    r = admin_client.get("/admin/status")
    r.raise_for_status()
    return r.json()["accounts"]


def _err(text: str) -> int:
    print(f"hub auth mail: {text}", file=sys.stderr)
    return 1


def cmd_auth_mail(
    args: object,
    *,
    admin_client: httpx.Client,
    config_dir: Path,
    gateway_factory: Callable[..., GmailGateway] = GmailGateway,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    print_fn: Callable[[str], None] = print,
    now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
) -> int:
    address = (getattr(args, "address", None) or "").strip().lower()
    if not address or "@" not in address or " " in address:
        return _err("нужен --address адрес@домен")
    try:
        imap_hp = _hostport(getattr(args, "imap", None), "imap")
        smtp_hp = _hostport(getattr(args, "smtp", None), "smtp")
    except ValueError as exc:
        return _err(str(exc))
    if (imap_hp is None) != (smtp_hp is None):
        return _err("обычный ящик: нужны оба ключа --imap host:port и --smtp host:port")
    name: str | None = (getattr(args, "name", None) or "").strip() or None
    raw_proxy: str | None = getattr(args, "proxy", None)
    account_id: int | None = getattr(args, "account", None)
    title: str | None = getattr(args, "title", None)
    db = getattr(args, "db", None) or "main"
    if db not in ("main", "work"):
        return _err("--db: main или work")
    proxy = normalize_proxy(getattr(args, "proxy", None))
    # 24.09 живой запуск: SMTP Gmail (465/587) из сети владельца закрыт и напрямую, и через прокси, IMAP открыт —
    # ящик подключается «только на приём»: SMTP не проверяется, ответ из хаба выключен (`send_enabled=False`).
    receive_only = bool(getattr(args, "receive_only", False))

    accounts = _accounts(admin_client)
    known_cfg: dict = {}
    if account_id is not None:
        found = next((a for a in accounts if a["id"] == account_id), None)
        if found is None or found["source"] != "mail":
            return _err(f"аккаунта почты {account_id} нет")
        last_ok = found.get("last_ok_ts")
        if last_ok is not None and now_ms() - last_ok < _HEARTBEAT_FRESH_MS:
            return _err(
                f"у аккаунта {account_id} свежий пульс — коннектор, похоже, работает; сначала "
                f"systemctl --user stop hub-connector@{account_id}"
            )
        key_path = config_dir / _CONNECTORS_SUBDIR / f"{account_id}.key"
        if key_path.exists():
            secret = key_path.read_text(encoding="utf-8").strip()
            r = admin_client.get("/account", headers={"Authorization": f"Bearer {secret}"})
            if r.status_code == 200:
                known_cfg = r.json().get("config") or {}
                known = (known_cfg.get("address") or "").lower()
                if known and known != address:
                    return _err(f"аккаунт {account_id} заведён на другой адрес — вход только тем же")
        if getattr(args, "db", None) or title or getattr(args, "receive_only", False):
            print_fn(
                "hub auth mail: --db/--title/--receive-only для существующего аккаунта не меняются (только "
                "пароль); --receive-only лишь пропускает проверку SMTP. Отправка включается и выключается "
                f"`hub account set {account_id} send_enabled=true` (или false)."
            )
    else:
        wanted_title = title or address
        dup = next((a for a in accounts if a["source"] == "mail" and a.get("title") == wanted_title), None)
        if dup is not None:
            return _err(
                f"аккаунт «{wanted_title}» уже есть (account_id={dup['id']}) — "
                f"повторный вход: --account {dup['id']}"
            )

    # Ф12-4: вид ящика — `--imap/--smtp`, при повторном входе — из конфига аккаунта
    plain: dict | None = None
    if imap_hp is not None or known_cfg.get("flavor") == FLAVOR_IMAP:
        if known_cfg and known_cfg.get("flavor", FLAVOR_GMAIL) != FLAVOR_IMAP:
            return _err(f"аккаунт {account_id} — ящик Gmail; --imap/--smtp для него не годятся")
        if imap_hp is None or smtp_hp is None:
            imap_hp = (str(known_cfg.get("imap_host") or ""), int(known_cfg.get("imap_port") or 993))
            smtp_hp = (str(known_cfg.get("smtp_host") or ""), int(known_cfg.get("smtp_port") or 465))
            if not imap_hp[0] or not smtp_hp[0]:
                return _err(f"у аккаунта {account_id} в конфиге нет хостов — укажи --imap и --smtp")
        if raw_proxy is None:
            if account_id is None or "proxy" not in known_cfg:
                return _err(
                    "обычный ящик: сеть — только явно: --proxy none (напрямую) или --proxy socks5://хост:порт"
                )
            proxy = normalize_proxy(known_cfg.get("proxy"))
        plain = {
            "flavor": FLAVOR_IMAP,
            "imap_host": imap_hp[0],
            "imap_port": imap_hp[1],
            "smtp_host": smtp_hp[0],
            "smtp_port": smtp_hp[1],
        }
    elif name is not None:
        return _err("--name — только для обычного ящика (с --imap/--smtp); у Gmail имя — --title")
    reasons = _AUTH_REASONS if plain is None else _PLAIN_AUTH_REASONS
    where = "Gmail" if plain is None else "почтовый сервер"

    if plain is None:
        # пароль приложения Google показывают группами через пробел — пробелы не часть пароля
        prompt = f"Пароль приложения Google для {address} (ввод не отображается): "
        password = "".join(getpass_fn(prompt).split())
    else:
        # пароль обычного ящика — как введён (пробел внутри может быть его частью), без перевода строки
        password = getpass_fn(f"Пароль ящика {address} (ввод не отображается): ").strip("\r\n")
    if not password:
        return _err(("пароль приложения" if plain is None else "пароль ящика") + " пуст — ничего не записано")

    try:
        info = asyncio.run(
            _verify(
                gateway_factory,
                address=address,
                password=password,
                proxy=proxy,
                check_smtp=not receive_only,
                plain=plain,
            )
        )
    except MailAuthError as exc:
        return _err(reasons.get(exc.kind, reasons["other"]) + " Ничего не записано.")
    except MailConfigError as exc:
        return _err(f"ящик не годится: {exc}. Ничего не записано.")
    except MailUnavailable as exc:
        if str(exc).startswith("smtp:") and plain is not None:
            return _err(
                "IMAP работает, но SMTP-сервер недоступен (нет сети до порта SMTP или прокси). Подключить "
                "ящик только на приём: добавь --receive-only. Ничего не записано."
            )
        if str(exc).startswith("smtp:"):
            return _err(
                "IMAP работает, но SMTP Gmail из этой сети недоступен (порты 465/587 закрыты). "
                "Подключить ящик только на приём (ответы — в Gmail): добавь --receive-only. "
                "Ничего не записано."
            )
        return _err(f"{where} недоступен (нет сети или прокси) — {type(exc).__name__}. Ничего не записано.")
    except (MailError, OSError) as exc:
        return _err(f"{where} недоступен (нет сети или прокси) — {type(exc).__name__}. Ничего не записано.")
    smtp_note = "SMTP не проверялся (только приём)" if receive_only else "SMTP"
    if plain is None:
        print_fn(f"вход проверен: IMAP (CONDSTORE: {'да' if info.condstore else 'нет'}) и {smtp_note}")
    else:
        print_fn(
            f"вход проверен: IMAP (IDLE: {'да' if info.idle else 'нет — опрос раз в 2 мин'}) и {smtp_note}"
        )

    if account_id is None and plain is not None:
        config = {
            "address": address,
            "aliases": [],
            "display_name": name or title or address,
            "provider": FLAVOR_IMAP,
            **plain,
            "proxy": proxy,
            "capabilities": sorted(info.capabilities),
            "reply_mode": "sender",
            "read_ack": False,  # Ф12-4, решение 4: хаб -> `\\Seen` для этого вида выключено
            "send_enabled": not receive_only,
        }
    elif account_id is None:
        config = {
            "address": address,
            "aliases": [],
            "display_name": title or address,
            "provider": "gmail",
            "proxy": proxy,
            "capabilities": sorted(info.capabilities),
            "reply_mode": "sender",
            "exclude_categories": list(DEFAULT_EXCLUDE_CATEGORIES),
            "send_enabled": not receive_only,
        }
    if account_id is None:
        r = admin_client.post(
            "/admin/accounts",
            json={"source": "mail", "title": title or address, "db": db, "config": config},
        )
        r.raise_for_status()
        body = r.json()
        account_id = body["account_id"]
        key_path = config_dir / _CONNECTORS_SUBDIR / f"{account_id}.key"
        connector_auth.write_secret_file(key_path, body["connector_secret"])
        print_fn(f"account_id={account_id} (db={db})")
        print_fn(f"секрет коннектора записан в {key_path}")

    creds_path = config_dir / _CONNECTORS_SUBDIR / f"{account_id}{CREDS_SUFFIX}"
    _write_json_atomic(creds_path, {"password": password})
    if plain is not None:
        print_fn(f"пароль ящика записан в {creds_path} (0600)")
        print_fn(
            f"дальше: `hub map add --account {account_id} --project P --all` (P — номер проекта из "
            "`hub project list`) и "
            f"`systemctl --user enable --now hub-connector@{account_id}`"
        )
        return 0
    print_fn(f"пароль приложения записан в {creds_path} (0600)")
    print_fn(
        "дальше: `hub map add --account "
        f"{account_id} --project P --all` (или --label ИМЯ) и "
        f"`systemctl --user enable --now hub-connector@{account_id}`"
    )
    return 0
