"""Точка входа демо в Docker: ядро хаба на выдуманных данных.

    python -m demo.serve          # запуск (по умолчанию): ядро, засев, живой трафик, код входа в лог
    python -m demo.serve code     # новый одноразовый адрес входа (код живёт 60 секунд)
    python -m demo.serve health   # проверка для HEALTHCHECK: ядро отвечает на /api/v1/healthz

Ядро (`hub core`) слушает только 127.0.0.1 и пускает только заголовок Host `127.0.0.1:<порт>` / `localhost:<порт>`
— так и должно быть, защиту ради демо не ослабляем. Из контейнера порт наружу публикует маленький TCP-мост
(`HUB_DEMO_LISTEN_PORT`, по умолчанию 8080): он ничего не читает и не меняет в запросах. Браузер на хосте ходит
на `localhost:<HUB_DEMO_PORT>` (compose публикует только на 127.0.0.1 хоста), значит Host проходит проверку ядра.

Данные и конфиг — в `HUB_DATA_DIR` и `HUB_CONFIG_DIR` (в compose это тома `/data` и `/config`). Первый запуск засевает
выдуманный мир (`demo/seed.py`) и пишет `demo-state.json`; следующие продолжают с теми же данными.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import signal
import socket
import sys
import time
import urllib.request
from dataclasses import asdict
from pathlib import Path

import httpx

from demo import seed
from hub.connectors.base import CoreClient
from hub.connectors.fake import FakeConnector, FakeSource, FaultPlan
from hub.connectors.press import Presser, PressResult

_logger = logging.getLogger("demo")

_INTERNAL_BASE_URL = "http://hub-internal/internal/v1"
_STARTUP_TIMEOUT_S = 60.0
_STATE_NAME = "demo-state.json"


def _data_dir() -> Path:
    return Path(os.environ.get("HUB_DATA_DIR", "/data"))


def _config_dir() -> Path:
    return Path(os.environ.get("HUB_CONFIG_DIR", "/config"))


def _core_port() -> int:
    return int(os.environ.get("HUB_DEMO_PORT", "8787"))


def _listen_port() -> int:
    return int(os.environ.get("HUB_DEMO_LISTEN_PORT", "8080"))


def _public_url() -> str:
    return os.environ.get("HUB_DEMO_PUBLIC_URL", f"http://localhost:{_core_port()}").rstrip("/")


def _ui_dir() -> Path:
    return Path(os.environ.get("HUB_DEMO_UI_DIR", str(Path(__file__).resolve().parents[1] / "ui" / "dist")))


def _admin_client() -> httpx.AsyncClient:
    secret = (_config_dir() / "admin.key").read_text(encoding="utf-8").strip()
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=str(_data_dir() / "internal.sock")),
        base_url=_INTERNAL_BASE_URL,
        headers={"Authorization": f"Bearer {secret}"},
    )


def _login_url(code: str) -> str:
    return f"{_public_url()}/login#code={code}"


# --- ожидание старта ядра ---------------------------------------------------------------------------------


async def _wait_core(proc: asyncio.subprocess.Process) -> None:
    key, sock = _config_dir() / "admin.key", _data_dir() / "internal.sock"
    deadline = time.monotonic() + _STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.returncode is not None:
            raise RuntimeError(f"hub core завершился при старте (код {proc.returncode})")
        if key.exists() and sock.exists():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(sock))
            except OSError:
                pass
            else:
                return
            finally:
                probe.close()
        await asyncio.sleep(0.1)
    raise TimeoutError(f"ядро не поднялось за {_STARTUP_TIMEOUT_S:.0f} с")


# --- TCP-мост наружу --------------------------------------------------------------------------------------


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def _forward(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
    try:
        up_r, up_w = await asyncio.open_connection("127.0.0.1", _core_port())
    except OSError:
        client_w.close()
        return
    await asyncio.gather(_pipe(client_r, up_w), _pipe(up_r, client_w))


async def _start_bridge() -> asyncio.Server:
    # Слушаем все интерфейсы контейнера; наружу порт публикует compose — только на 127.0.0.1 хоста.
    return await asyncio.start_server(_forward, "0.0.0.0", _listen_port())  # noqa: S104


# --- живой трафик -----------------------------------------------------------------------------------------


class Pumps:
    """Fake-коннекторы двух аккаунтов: отвечают на отправки из интерфейса, принимают отметки и реакции."""

    def __init__(self, tg: CoreClient, mail: CoreClient) -> None:
        self.clients = {"telegram": tg, "mail": mail}
        self.connectors = {
            name: FakeConnector(client, FakeSource(idempotent=False), plan=FaultPlan())
            for name, client in self.clients.items()
        }
        self.presser = Presser(
            tg,
            self._press,
            mono=time.monotonic,
            sleep=asyncio.sleep,
            is_blocked=lambda: False,
            on_auth_required=self._noop,
            on_unauthorized=lambda: None,
        )

    @staticmethod
    async def _noop() -> None:
        return None

    @staticmethod
    async def _press(item: dict) -> PressResult:
        return PressResult("done", result={"message": "Report sent to your chat", "url": "https://example.com/report"})

    async def step(self) -> None:
        for name, connector in self.connectors.items():
            for stage in ("step_outbox", "sync", "step_read_ack", "step_reactions", "step_msg_ops"):
                try:
                    await getattr(connector, stage)()
                except Exception:
                    _logger.exception("demo: %s аккаунта %s упал", stage, name)
        try:
            await self.presser.step()
        except Exception:
            _logger.exception("demo: исполнитель нажатий упал")


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> bool:
    try:
        async with asyncio.timeout(seconds):
            await stop.wait()
    except TimeoutError:
        return False
    return True


async def _traffic_loop(pumps: Pumps, stop: asyncio.Event) -> None:
    """Каждые 1–2 секунды — ответы на отправки из интерфейса; раз в 25–45 секунд — новое входящее."""
    chats = {c[0]: c for c in (*seed.TELEGRAM_CHATS, *seed.MAIL_CHATS)}
    next_incoming = time.monotonic() + 30
    while not stop.is_set():
        await pumps.step()
        if time.monotonic() >= next_incoming:
            next_incoming = time.monotonic() + random.uniform(25, 45)  # noqa: S311 — не криптография
            ext = random.choice(list(seed.FILLER_LINES))  # noqa: S311
            who, texts = seed.FILLER_LINES[ext]
            chat = chats[ext]
            line = (who, random.choice(texts), 0)  # noqa: S311
            client = pumps.clients["mail" if chat[1] == "mail_thread" else "telegram"]
            try:
                await client.push_events([seed.event(chat, line, seed.now_ms(), seed.uid())])
            except Exception:
                _logger.exception("demo: входящее не доставлено")
        if await _sleep_or_stop(stop, random.uniform(1.0, 2.0)):  # noqa: S311
            return


async def _work_loop(st: seed.DemoState, port: int, stop: asyncio.Event) -> None:
    """Раз в 10 секунд — подтверждение состояния сессий и новое событие в ленте первой (как делал бы хост)."""
    tick = 0
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}", headers={"Authorization": f"Bearer {st.host_token}"}
    ) as host:
        while not await _sleep_or_stop(stop, 10.0):
            event = dict(seed.LIVE_EVENTS[tick % len(seed.LIVE_EVENTS)])
            tick += 1
            try:
                await seed.host_state(host, st.running_session, "running")
                await seed.host_state(host, st.idle_session, "idle")
                await seed.host_events(host, st.running_session, [event])
            except httpx.HTTPError as exc:
                _logger.warning("demo: тик сессий не прошёл: %s", exc)


# --- состояние и запуск -----------------------------------------------------------------------------------


def _load_state() -> seed.DemoState | None:
    path = _data_dir() / _STATE_NAME
    if not path.is_file():
        return None
    return seed.DemoState(**json.loads(path.read_text(encoding="utf-8")))


def _save_state(st: seed.DemoState) -> None:
    path = _data_dir() / _STATE_NAME
    path.write_text(json.dumps(asdict(st), indent=2), encoding="utf-8")
    path.chmod(0o600)


async def run() -> None:
    port = _core_port()
    env = {**os.environ, "HUB_DATA_DIR": str(_data_dir()), "HUB_CONFIG_DIR": str(_config_dir())}
    cmd = [sys.executable, "-c", "from hub.cli import main; main()", "core", "--port", str(port)]
    if _ui_dir().is_dir():
        cmd += ["--ui-dir", str(_ui_dir())]
    else:
        print(f"ВНИМАНИЕ: нет собранного интерфейса ({_ui_dir()}) — будет только API.", flush=True)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    print(f"Запускаю hub core на 127.0.0.1:{port} (данные {_data_dir()}, конфиг {_config_dir()})", flush=True)
    proc = await asyncio.create_subprocess_exec(*cmd, env=env)  # noqa: S603
    tasks: list[asyncio.Task] = []
    bridge: asyncio.Server | None = None
    admin: httpx.AsyncClient | None = None
    public = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")
    try:
        await _wait_core(proc)
        admin = _admin_client()
        st = _load_state()
        sock = _data_dir() / "internal.sock"
        if st is None:
            print("Первый запуск: засеваю выдуманные данные (Acme, Globex, Initech) ...", flush=True)
            st, tg, mail = await seed.seed_world(
                admin, public, socket_path=sock, port=port, work_root=_data_dir() / "demo-work"
            )
            _save_state(st)
        else:
            tg = CoreClient(socket_path=str(sock), secret=st.tg_secret)
            mail = CoreClient(socket_path=str(sock), secret=st.mail_secret)
        await seed.name_machines(admin)
        pumps = Pumps(tg, mail)
        tasks = [
            asyncio.create_task(_traffic_loop(pumps, stop)),
            asyncio.create_task(_work_loop(st, port, stop)),
        ]
        bridge = await _start_bridge()
        print(flush=True)
        print("Хаб готов. Откройте адрес входа (код одноразовый и живёт 60 секунд):", flush=True)
        print(f"  {_login_url(await seed.login_code(admin))}", flush=True)
        print("Новый адрес: docker compose exec hub python -m demo.serve code", flush=True)
        stop_task = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait({stop_task, asyncio.create_task(proc.wait())}, return_when=asyncio.FIRST_COMPLETED)
        if stop_task not in done:
            print("hub core завершился — останавливаю демо.", flush=True)
        stop.set()
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        if bridge is not None:
            bridge.close()
        if admin is not None:
            await admin.aclose()
        await public.aclose()
        if proc.returncode is None:
            proc.terminate()
            try:
                async with asyncio.timeout(10):
                    await proc.wait()
            except TimeoutError:
                proc.kill()
                await proc.wait()
        print("Демо остановлено.", flush=True)


async def new_code() -> int:
    async with _admin_client() as admin:
        print(_login_url(await seed.login_code(admin)))
    return 0


def health() -> int:
    """0 — ядро отвечает на /api/v1/healthz. Заголовок Host по умолчанию — `127.0.0.1:<порт>`: его ядро пускает."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{_core_port()}/api/v1/healthz", timeout=3) as r:  # noqa: S310
            return 0 if r.status == 200 else 1
    except OSError:
        return 1


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    command = args[0] if args else "run"
    if command == "health":
        return health()
    logging.basicConfig(level=logging.WARNING)
    if command == "code":
        return asyncio.run(new_code())
    if command == "run":
        asyncio.run(run())
        return 0
    print("использование: python -m demo.serve [run|code|health]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
