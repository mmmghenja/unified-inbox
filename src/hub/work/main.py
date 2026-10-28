"""Команды hub-work: `hub work setup|status|run|guard` (Ф9-1, 9a, F9; поправки П10, П14, 9a-a).

`setup` запускает владелец (боевые `~/.config/hub`, `~/.local/share/hub` агенту-разработчику закрыты). Токен ядра
не печатается и не попадает в аргументы командной строки — только в файл 0600 (запись атомарно).

НИКАКИХ команд, которые решают запрос сессии (П3): это делает только владелец в UI."""

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import signal
from datetime import datetime
from pathlib import Path

import httpx

from hub import session_rules
from hub.agent.core_api import CoreApi

from . import files, session
from .host import EXIT_UNAUTHORIZED, WorkHost

_logger = logging.getLogger("hub.work")

DEFAULT_URL = "http://127.0.0.1:8787/api/v1"
CLIENT_NAME = "work:host"
EXIT_LOCKED = 75


class SetupError(Exception):
    """Понятная ошибка настройки — CLI печатает её и выходит с ненулевым кодом."""


class WorkCoreApi(CoreApi):
    """Клиент ядра хоста: как у hub-agent (`trust_env=False`, 401 -> перечитать токен), таймаут — с запасом на
    длинное ожидание `/work/host/wait`."""

    def __init__(
        self, *, token_file: Path, base_url: str, transport=None, wait_timeout_s: float = 25.0
    ) -> None:
        super().__init__(
            token_file=token_file, base_url=base_url, transport=transport, timeout=wait_timeout_s + 20
        )
        self._http.headers["X-Hub-Client"] = CLIENT_NAME


# --- hub work setup -----------------------------------------------------------------------------------------


def _old_token_to_revoke(admin: httpx.Client, config_dir: Path) -> int | None:
    meta = files.read_json(files.token_meta_path(config_dir))
    old_id = meta.get("token_id") if meta else None
    if not isinstance(old_id, int) or isinstance(old_id, bool):
        return None
    r = admin.get("/admin/tokens")
    r.raise_for_status()
    for token in r.json():
        if token["id"] == old_id and token["name"] == files.TOKEN_NAME and not token["revoked"]:
            return old_id
    return None


def run_setup(
    admin: httpx.Client,
    *,
    config_dir: Path,
    data_dir: Path,
    environ: dict | None = None,
    home: Path | None = None,
    auth_checker=None,
    base_url: str = DEFAULT_URL,
    hub_path: str | None = None,
) -> str:
    """F9: токен `work:host` (`work_host` + `read`, background, все проекты) -> записать -> отозвать прежний;
    каталоги `<data_dir>/work/` (0700); файл окружения юнита из текущей сессии владельца (П14); конфиг по
    умолчанию; `claude auth status`."""
    environ = dict(os.environ if environ is None else environ)
    home = Path.home() if home is None else home
    files.private_dir(files.config_work_dir(config_dir))
    files.private_dir(files.data_work_dir(data_dir))
    old_id = _old_token_to_revoke(admin, config_dir)
    r = admin.post(
        "/admin/tokens",
        json={
            "name": files.TOKEN_NAME,
            "kind": "background",
            "scopes": list(files.TOKEN_SCOPES),
            "project_ids": None,
            "ttl_days": None,
        },
    )
    if r.status_code != 200:
        raise SetupError(f"ядро не создало токен: HTTP {r.status_code}")
    body = r.json()
    files.write_secret(files.token_path(config_dir), body["token"])
    files.atomic_write(files.token_meta_path(config_dir), json.dumps({"token_id": body["token_id"]}) + "\n")
    lines = [
        f"Токен hub-work: id={body['token_id']}, скоупы {' '.join(files.TOKEN_SCOPES)}, все проекты "
        f"(background).",
        f"Токен записан в {files.token_path(config_dir)} (0600).",
    ]
    if old_id is not None and old_id != body["token_id"]:
        rr = admin.post(f"/admin/tokens/{old_id}/revoke")
        lines.append(
            f"Прежний токен hub-work (id={old_id}) отозван."
            if rr.status_code == 200
            else f"Прежний токен id={old_id} отозвать не удалось (HTTP {rr.status_code})."
        )
    if files.write_default_config(config_dir):
        lines.append(f"Конфиг по умолчанию: {files.config_path(config_dir)}.")
    unit_env = files.unit_environment(environ, home=home)
    env_file = files.write_env_file(config_dir, unit_env)
    lines.append(f"Окружение юнита: {env_file} (ключи: {', '.join(sorted(unit_env)) or 'нет'}).")
    if "SSH_AUTH_SOCK" not in unit_env:
        lines.append("  ВНИМАНИЕ: SSH_AUTH_SOCK не найден — ssh и git по ssh из рабочих сессий не пойдут.")
    config = files.load_config(config_dir)
    path = files.claude_path(config)
    if auth_checker is None:
        from hub.engine import claude_run as wake_claude

        auth_checker = wake_claude.auth_status
    ok, auth_text = auth_checker(path, session.build_env(environ, claude=path, task_id=0))
    lines += [
        f"claude auth status: {auth_text}" + ("" if ok else " — рабочие сессии НЕ запустятся"),
        f"Каталог данных: {files.data_work_dir(data_dir)} (0700).",
        f"Правила сессий хаба: {session_rules.common_rules_path(home)} и <workdir>/.claude/hub.md.",
        "",
        "Дальше: systemctl --user enable --now hub-work.service",
    ]
    return "\n".join(lines)


# --- hub work status ----------------------------------------------------------------------------------------


def _fmt_ms(ts) -> str:
    if not isinstance(ts, int):
        return "—"
    return datetime.fromtimestamp(ts / 1000).strftime("%d.%m %H:%M:%S")


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def status_text(*, data_dir: Path, now_s: float) -> str:
    """F9: пауза по лимиту, вход движков, предупреждения о правилах, живые процессы сессий хаба."""
    state = files.load_state(data_dir)
    lines: list[str] = []
    if state is None:
        lines.append("hub work: состояния нет (хост ещё не запускался или state.json битый)")
        return "\n".join(lines)
    now_ms = int(now_s * 1000)
    # Ф41-2б П6: записи паузы и входа — на движок (`pauses`, `auths`).
    for engine, record in sorted((_dict(state.get("pauses"))).items()):
        if isinstance(record, dict) and record.get("until", 0) > now_ms:
            lines.append(f"ПАУЗА {engine} до {_fmt_ms(record['until'])}: {record.get('reason')}")
    for engine, record in sorted((_dict(state.get("auths"))).items()):
        if isinstance(record, dict) and not record.get("ok", True):
            lines.append(f"{engine} НЕ запускается — {record.get('text')}")
    overage = state.get("overage")
    if isinstance(overage, dict) and overage.get("until", 0) > now_ms:
        lines.append(
            f"ПАУЗА до {_fmt_ms(overage['until'])}: работа идёт сверх подписки — сессии Claude Code ждут"
        )
    for pid, warnings in sorted((state.get("rules_warnings") or {}).items()):
        for warning in warnings if isinstance(warnings, list) else []:
            lines.append(f"ВНИМАНИЕ, правила сессии проекта {pid}: {warning}")
    lines.append(f"хост: pid {state.get('pid')}, последний круг {_fmt_ms(state.get('last_loop_ts'))}")
    held = _dict(state.get("held"))
    lines.append("сессии хаба с живым процессом:" if held else "живых процессов сессий хаба нет")
    for session_id, info in sorted(held.items()):
        lines.append(
            f"  сессия хаба #{session_id} (проект {info.get('project_id')}, {info.get('engine')}): "
            f"pid {info.get('pid')}, {'идёт ход' if info.get('busy') else 'ждёт'}, запросов ждут: "
            f"{info.get('pending')}, разговор {info.get('engine_session_id')}"
        )
    return "\n".join(lines)


# --- hub work run -------------------------------------------------------------------------------------------


def default_notifier(title: str, text: str, urgency: str) -> None:
    from hub import watch

    # Ф33-2: уведомление уходит в ядро, показывает его `hub notify` там, где экран; нет ядра — строка в stderr
    watch.notify_core_or_direct(title, text, urgency=urgency, quiet=True)


def acquire_lock(data_dir: Path):
    """П10: второй `hub work run` не стартует. Возвращает открытый файл (держать до выхода) или `None`."""
    files.private_dir(files.data_work_dir(data_dir))
    fd = os.open(files.lock_path(data_dir), os.O_RDWR | os.O_CREAT, 0o600)
    handle = os.fdopen(fd, "r+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def build(
    *,
    config_dir: Path,
    data_dir: Path,
    base_url: str,
    core_transport=None,
    notifier=None,
    environ=None,
    auth_checker=None,
    home: Path | None = None,
    guard_path: str | None = None,
    wall=None,
    monotonic=None,
) -> WorkHost:
    config = files.load_config(config_dir)
    core = WorkCoreApi(
        token_file=files.token_path(config_dir),
        base_url=base_url,
        transport=core_transport,
        wait_timeout_s=float(config.get("wait_timeout_s", 25)),
    )
    kwargs = {}
    if wall is not None:
        kwargs["wall"] = wall
    if monotonic is not None:
        kwargs["monotonic"] = monotonic
    return WorkHost(
        core=core,
        config=config,
        config_dir=config_dir,
        data_dir=data_dir,
        notifier=notifier or default_notifier,
        environ=environ,
        auth_checker=auth_checker,
        home=home,
        guard_path=guard_path,
        **kwargs,
    )


def run(*, config_dir: Path, data_dir: Path, base_url: str) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if files.read_secret(files.token_path(config_dir)) is None:
        _logger.error("work: нет токена ядра — hub work setup")
        return EXIT_UNAUTHORIZED
    lock = acquire_lock(data_dir)
    if lock is None:
        _logger.error("work: другой hub work run уже работает (%s)", files.lock_path(data_dir))
        return EXIT_LOCKED
    host = build(config_dir=config_dir, data_dir=data_dir, base_url=base_url)
    _logger.info("work: старт — ядро %s, claude %s", base_url, files.claude_path(host.config))

    async def _main() -> int:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        try:
            return await host.run(stop)
        finally:
            await host.core.aclose()

    try:
        return asyncio.run(_main())
    finally:
        with contextlib.suppress(OSError):
            lock.close()
