"""Ф33-1 (12а К1): сторож «сессия умерла» после выхода MCP-сервера.

MCP-сервер сессии Claude Code узнаёт о конце по закрытию stdin или SIGTERM — а сама сессия в этот момент
обычно ещё жива (закрывает свои серверы и ждёт их выхода). `gone` по К1 шлётся ТОЛЬКО при мёртвом процессе
сессии, поэтому ждать смерти в самом сервере нельзя: сессия ждёт его, он — её. Сервер запускает этот сторож
отдельной группой процессов и выходит; сторож до `--wait-s` секунд смотрит `/proc` этой машины и, когда
процесс сессии (pid + `starttime`) умер, шлёт ядру `POST /subs/beat` с `gone: true`. Сессия жива всё окно
(`/mcp`, переподключение сервера) — выходит молча. Токен — из профиля, в аргументы не попадает."""

import argparse
import sys
import time
from pathlib import Path

import anyio

from hub import session_registry

from .client import HubClient

WAIT_S = 60.0
POLL_S = 0.5
SEND_S = 5.0  # одна попытка
TRIES = 3  # ревью П1: до 3 попыток за ~30 с
PAUSE_S = 10.0


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="hub.mcp_server.gone_watch")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--config-dir", required=True, type=Path)
    parser.add_argument("--url", required=True)
    parser.add_argument("--pid", required=True, type=int)
    parser.add_argument("--proc-start", required=True, type=int)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--wait-s", type=float, default=WAIT_S)
    return parser.parse_args(argv)


def wait_dead(
    pid: int, proc_start: int, wait_s: float, *, sleep=time.sleep, monotonic=time.monotonic
) -> bool:
    """Процесс сессии умер за окно `wait_s` (`True`) или жив всё окно (`False`)."""
    deadline = monotonic() + wait_s
    while session_registry.process_alive(pid, proc_start):
        if monotonic() >= deadline:
            return False
        sleep(POLL_S)
    return True


async def send_gone_retry(client, body: dict, *, sleep=anyio.sleep, tries: int = TRIES) -> bool:
    """`POST /subs/beat` с `gone`: до `tries` попыток по `SEND_S` с паузой `PAUSE_S` (туннель мог моргнуть,
    ядро — перезапускаться). Дошло — `True`."""
    for attempt in range(tries):
        if attempt:
            await sleep(PAUSE_S)
        try:
            with anyio.fail_after(SEND_S):
                await client.request("POST", "/subs/beat", json=body)
        except Exception:  # noqa: BLE001, S112 — следующая попытка
            continue
        return True
    return False


async def send_gone(args: argparse.Namespace) -> bool:
    client = HubClient(profile=args.profile, config_dir=args.config_dir, base_url=args.url)
    session = {
        "pid": args.pid,
        "proc_start": args.proc_start,
        "session_id": args.session_id,
        "host": args.host,
    }
    try:
        return await send_gone_retry(client, {"session": session, "gone": True})
    finally:
        await client.aclose()


def main(argv: list[str] | None = None) -> int:
    args = parse(sys.argv[1:] if argv is None else argv)
    if not wait_dead(args.pid, args.proc_start, args.wait_s):
        return 0
    try:
        sent = anyio.run(send_gone, args)
    except Exception:  # noqa: BLE001 — не дошло: снимет сосед той же машины или 12 ч без связи
        return 1
    return 0 if sent else 1


if __name__ == "__main__":
    raise SystemExit(main())
