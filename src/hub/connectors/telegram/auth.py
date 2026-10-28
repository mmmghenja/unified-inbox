"""`hub auth tg [--account ID | --new] [--title T] [--proxy URL]` — вход в Telegram. Спецификация:
`docs/architecture.md`. Запускает ТОЛЬКО владелец — секреты не читает и не
пишет никто другой; ничего секретное не печатается и не логируется.

Всё, что требует сети/интерактивного ввода, инжектируется параметрами (`admin_client`, `gateway_factory`,
`input_fn`, `getpass_fn`, `confirm_fn`) — тесты подменяют их целиком, живого Telegram здесь никогда не бывает.
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import inspect
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from ...core import connector_auth
from .connector import parse_proxy
from .gateway import TgGateway

DEFAULT_PROXY = "socks5://127.0.0.1:10808"
DEVICE_MODEL = "hub"
SYSTEM_VERSION = "linux"
APP_VERSION = "0.1"

_CONNECTORS_SUBDIR = "connectors"
_CREDS_SUFFIX = ".telegram.json"
# Мелочь из отчёта исполнителя (round 2): пульс свежее этого -> коннектор, похоже, ещё работает — переписывать
# креды/сессию у него из-под ног нельзя (`tg.session` открыт тем процессом).
_HEARTBEAT_FRESH_MS = 3 * 60 * 1000


def _write_json_atomic(path: Path, payload: dict) -> None:
    """0600, атомарно: временный файл С УНИКАЛЬНЫМ ИМЕНЕМ (`tempfile.mkstemp` в том же каталоге, мелочь из
    отчёта исполнителя round 2 — фиксированное имя `<path>.tmp` могло схлопнуться при двух параллельных
    запусках `hub auth tg`) + `os.replace` (спека, шаг 2)."""
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


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _do_login(gateway: TgGateway, *, input_fn, getpass_fn) -> object:
    await gateway.connect()
    try:
        await _maybe_await(
            gateway.client.start(
                phone=lambda: input_fn("Телефон (+7...): "),
                code_callback=lambda: input_fn("Код из Telegram: "),
                password=lambda: getpass_fn("Пароль двухфакторной аутентификации (Enter, если нет): "),
            )
        )
        me = await _maybe_await(gateway.get_me())
    finally:
        with contextlib.suppress(Exception):
            await _maybe_await(gateway.disconnect())
    return me


def _connector_stop_hint(admin_client: httpx.Client, account_id: int, *, now_ms: int) -> str | None:
    """Мелочь из отчёта исполнителя (round 2): если у аккаунта свежий пульс (< `_HEARTBEAT_FRESH_MS`),
    коннектор, похоже, уже работает — `hub auth tg` не должен переписывать креды/сессию у него из-под ног.
    Возвращает команду для остановки, либо `None`, если можно продолжать."""
    r = admin_client.get("/admin/status")
    r.raise_for_status()
    for account in r.json()["accounts"]:
        if account["id"] != account_id:
            continue
        last_ok_ts = account.get("last_ok_ts")
        if last_ok_ts is not None and now_ms - last_ok_ts < _HEARTBEAT_FRESH_MS:
            return f"systemctl --user stop hub-connector@{account_id}"
        return None
    return None


def _telegram_account_ids(admin_client: httpx.Client) -> list[int]:
    """Аккаунты `telegram` по возрастанию id. Без `--account` и `--new` единственный из них переиспользуется
    вместо создания нового — иначе КАЖДЫЙ повторный запуск плодил бы отдельный аккаунт (спека 1b, тест 13);
    второй аккаунт заводится только явным `--new` (план 11.33)."""
    r = admin_client.get("/admin/status")
    r.raise_for_status()
    return sorted(a["id"] for a in r.json()["accounts"] if a["source"] == "telegram")


def _display_name(me: object) -> str:
    """Имя, под которым вошли — БЕЗ телефона (спека: «напечатать, под кем вошли, без телефона»)."""
    first = getattr(me, "first_name", None)
    last = getattr(me, "last_name", None)
    parts = [p for p in (first, last) if p]
    if parts:
        return " ".join(parts)
    username = getattr(me, "username", None)
    return f"@{username}" if username else "(без имени)"


def cmd_auth_tg(
    args: object,
    *,
    admin_client: httpx.Client,
    config_dir: Path,
    data_dir: Path,
    gateway_factory: type = TgGateway,
    client_factory=None,
    input_fn=input,
    getpass_fn=getpass.getpass,
    confirm_fn=None,
    print_fn=print,
) -> int:
    confirm_fn = confirm_fn or (lambda prompt: input_fn(prompt).strip().lower() in ("y", "yes", "д", "да"))
    account_id: int | None = getattr(args, "account", None)
    explicit_proxy: str | None = getattr(args, "proxy", None)
    title: str | None = getattr(args, "title", None)
    create_new = bool(getattr(args, "new", False))
    proxy = explicit_proxy or DEFAULT_PROXY
    is_existing_account = account_id is not None

    if create_new and account_id is not None:
        print("hub auth tg: --new и --account вместе нельзя", file=sys.stderr)
        return 1
    existing_ids = _telegram_account_ids(admin_client)
    if account_id is not None and account_id not in existing_ids:
        # Без этой проверки креды и сессия легли бы под id, которого нет в ядре (или у чужого источника).
        known = ", ".join(map(str, existing_ids)) or "нет"
        print(
            f"hub auth tg: аккаунта telegram {account_id} нет (есть: {known}); новый — `hub auth tg --new`",
            file=sys.stderr,
        )
        return 1
    if account_id is None and not create_new:
        if len(existing_ids) > 1:
            print(
                f"hub auth tg: аккаунтов telegram несколько ({', '.join(map(str, existing_ids))}) — "
                "укажите --account <id> или --new",
                file=sys.stderr,
            )
            return 1
        if existing_ids:
            account_id = existing_ids[0]
            is_existing_account = True
            print_fn(f"account_id={account_id} (аккаунт telegram уже существует, использую его)")
    if title is None:
        # Без --title аккаунт создавался бы безымянным (title=None) — в уведомлениях hub-watch/`hub status`
        # аккаунт был бы виден без имени (находка 8 приёмки куска 1b); второй — с номером, чтобы «Откуда» в UI
        # («Telegram · <title>») их различал.
        title = f"Telegram {len(existing_ids) + 1}" if existing_ids else "Telegram"

    if is_existing_account and explicit_proxy:
        # Мелочь из отчёта исполнителя (round 2): --proxy для СУЩЕСТВУЮЩЕГО аккаунта используется ТОЛЬКО
        # для этого входа (гейтвей ниже) — в `config_json` аккаунта (там, откуда его берёт `hub connector`
        # при следующем запуске) он не пишется; менять config аккаунта эта команда не умеет.
        print_fn(
            f"hub auth tg: --proxy={explicit_proxy!r} использован только для ЭТОГО входа — "
            f"config аккаунта {account_id} не изменён (там по-прежнему прежний proxy)."
        )

    if account_id is not None:
        stop_hint = _connector_stop_hint(admin_client, account_id, now_ms=int(time.time() * 1000))
        if stop_hint is not None:
            print_fn(
                f"hub auth tg: у аккаунта {account_id} свежий пульс — коннектор, похоже, ещё работает; "
                f"сначала остановите его: {stop_hint}"
            )
            return 1

    if account_id is None:
        r = admin_client.post(
            "/admin/accounts",
            json={
                "source": "telegram",
                "title": title,
                "db": "main",
                "config": {
                    "proxy": proxy,
                    "device_model": DEVICE_MODEL,
                    "system_version": SYSTEM_VERSION,
                    "app_version": APP_VERSION,
                    "idempotent_send": True,
                },
            },
        )
        r.raise_for_status()
        body = r.json()
        account_id = body["account_id"]
        key_path = config_dir / _CONNECTORS_SUBDIR / f"{account_id}.key"
        connector_auth.write_secret_file(key_path, body["connector_secret"])
        print_fn(f"account_id={account_id}")
        print_fn(f"секрет коннектора записан в {key_path}")

    creds_path = config_dir / _CONNECTORS_SUBDIR / f"{account_id}{_CREDS_SUFFIX}"
    if creds_path.exists() and not confirm_fn(f"{creds_path} уже существует — заменить? [y/N] "):
        print_fn("отменено")
        return 1

    api_id_raw = input_fn("api_id: ").strip()
    try:
        api_id = int(api_id_raw)
    except ValueError:
        print("api_id должен быть числом", file=sys.stderr)
        return 1
    api_hash = getpass_fn("api_hash: ").strip()
    _write_json_atomic(creds_path, {"api_id": api_id, "api_hash": api_hash})

    try:
        proxy_tuple = parse_proxy({"proxy": proxy})
    except Exception as exc:  # noqa: BLE001 — понятное сообщение вместо трассировки
        print(f"hub auth tg: {exc}", file=sys.stderr)
        return 1

    session_dir = data_dir / "telegram" / str(account_id)
    session_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    session_path = session_dir / "tg"  # SQLiteSession сама допишет ".session" (см. отчёт — факт-чек)

    gateway_kwargs: dict[str, Any] = {
        "session_path": str(session_path),
        "api_id": api_id,
        "api_hash": api_hash,
        "proxy": proxy_tuple,
        "device_model": DEVICE_MODEL,
        "system_version": SYSTEM_VERSION,
        "app_version": APP_VERSION,
    }
    if client_factory is not None:
        gateway_kwargs["client_factory"] = client_factory
    gateway = gateway_factory(**gateway_kwargs)

    me = asyncio.run(_do_login(gateway, input_fn=input_fn, getpass_fn=getpass_fn))

    session_file = session_dir / "tg.session"
    if session_file.exists():
        os.chmod(session_file, 0o600)
    if session_dir.stat().st_mode & 0o077:
        os.chmod(session_dir, 0o700)

    print_fn(f"вход выполнен: {_display_name(me)}")
    print_fn(f"следующий шаг: systemctl --user enable --now hub-connector@{account_id}")
    return 0
