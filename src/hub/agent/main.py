"""Команды дежурного агента: `hub agent setup|run|status`. Спека 7b, B2 и поправки П7 (повторный setup не
гасит агента), К4 (каталог агента). Ф57-1: модели нет — `hub agent check` и `hub auth deepseek` убраны.

`setup` запускает владелец (боевые `~/.config/hub`, `~/.local/share/hub` агенту-разработчику закрыты). Токен ядра
не печатается и не попадает в аргументы командной строки — только в файл 0600 (запись атомарно).
"""

import asyncio
import json
import logging
import signal
from datetime import datetime
from pathlib import Path

import httpx

from . import files
from .core_api import CoreApi
from .dispatcher import Agent

_logger = logging.getLogger("hub.agent")


# --- hub agent setup ----------------------------------------------------------------------------------------


class SetupError(Exception):
    """Понятная ошибка настройки — CLI печатает её и выходит с ненулевым кодом."""


def _old_token_to_revoke(admin: httpx.Client, config_dir: Path) -> int | None:
    """П7 (как `hub mcp setup`): отзывать только токен из `token.json`, и только если его имя ровно
    `agent:triage` и он ещё не отозван — испорченный файл не должен отозвать чужой токен."""
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


def run_setup(admin: httpx.Client, *, config_dir: Path, data_dir: Path) -> str:
    """Токен `background` `agent:triage` со скоупами `read triage` через админский сокет: создать -> записать
    атомарно -> отозвать прежний (по id и имени). Конфиг по умолчанию, если его нет. Каталог данных агента
    (0700) — сразу (К4: иначе `ReadWritePaths` юнита роняет его с 226/NAMESPACE)."""
    files.private_dir(files.config_agent_dir(config_dir))
    files.private_dir(files.data_agent_dir(data_dir))
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
    config_written = files.write_default_config(config_dir)

    lines = [
        f"Токен агента: id={body['token_id']}, скоупы {' '.join(files.TOKEN_SCOPES)}, "
        "все проекты (background).",
        f"Токен записан в {files.token_path(config_dir)} (0600).",
    ]
    if old_id is not None and old_id != body["token_id"]:
        rr = admin.post(f"/admin/tokens/{old_id}/revoke")
        if rr.status_code == 200:
            lines.append(f"Прежний токен агента (id={old_id}) отозван.")
        else:
            lines.append(f"Прежний токен id={old_id} отозвать не удалось (HTTP {rr.status_code}).")
    if config_written:
        lines.append(f"Конфиг по умолчанию: {files.config_path(config_dir)}.")
    lines += [
        f"Каталог состояния: {files.data_agent_dir(data_dir)} (0700).",
        "",
        "Дальше: systemctl --user enable --now hub-agent.service",
    ]
    return "\n".join(lines)


# --- hub agent status ---------------------------------------------------------------------------------------


def _fmt_ms(ts: int | None) -> str:
    if not isinstance(ts, int):
        return "—"
    return datetime.fromtimestamp(ts / 1000).strftime("%d.%m %H:%M:%S")


def status_text(data_dir: Path, *, max_seq: int | None, now_ms: int) -> str:
    """Из state: курсор, отставание от `max_seq`, счётчики, последние ошибки — без текстов."""
    state = files.load_state(data_dir)
    if state is None:
        return "hub agent: состояния нет (агент ещё не запускался или state.json битый)"
    seq = state.get("seq")
    lag = max_seq - seq if isinstance(max_seq, int) and isinstance(seq, int) else None
    lines = [
        f"курсор: {seq} (max_seq {max_seq if max_seq is not None else '?'}; отставание "
        f"{lag if lag is not None else '?'})",
        f"последний успешный опрос: {_fmt_ms(state.get('last_ok_ts'))}",
        "счётчики: " + json.dumps(state.get("counters") or {}, ensure_ascii=False, sort_keys=True),
    ]
    errors = state.get("errors") or []
    if errors:
        lines.append("последние ошибки:")
        for err in errors[-10:]:
            code = f" HTTP {err.get('code')}" if err.get("code") is not None else ""
            lines.append(f"  {_fmt_ms(err.get('ts'))} {err.get('where')}: {err.get('cls')}{code}")
    return "\n".join(lines)


# --- hub agent run ------------------------------------------------------------------------------------------


def default_notifier(title: str, text: str, urgency: str) -> None:
    from hub import watch

    # Ф33-2: уведомление уходит в ядро, показывает его `hub notify` там, где экран; нет ядра — строка в stderr
    watch.notify_core_or_direct(title, text, urgency=urgency, quiet=True)


def build(
    *,
    config_dir: Path,
    data_dir: Path,
    base_url: str,
    core_transport=None,
    notifier=None,
    wall=None,
    mono=None,
) -> Agent:
    config = files.load_config(config_dir)
    core = CoreApi(token_file=files.token_path(config_dir), base_url=base_url, transport=core_transport)
    kwargs = {}
    if wall is not None:
        kwargs["wall"] = wall
    if mono is not None:
        kwargs["mono"] = mono
    return Agent(
        core=core,
        config=config,
        data_dir=data_dir,
        notifier=notifier or default_notifier,
        **kwargs,
    )


def run(*, config_dir: Path, data_dir: Path, base_url: str) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if files.read_secret(files.token_path(config_dir)) is None:
        _logger.error("agent: нет токена ядра — hub agent setup")
        return 78
    files.private_dir(files.data_agent_dir(data_dir))
    agent = build(config_dir=config_dir, data_dir=data_dir, base_url=base_url)
    _logger.info("agent: старт — карточки по правилам, без модели")

    async def _main() -> int:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        try:
            return await agent.run(stop)
        finally:
            await agent.core.aclose()

    return asyncio.run(_main())
