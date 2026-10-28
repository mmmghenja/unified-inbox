"""Приватный сервер OpenCode и клиент его HTTP API (Ф16, 3.3, F1, F3, F6, F7, F9, F10, П10).

`OcServer` — один процесс `opencode serve` на свободном порту 127.0.0.1 со своим паролем basic auth и своим
конфигом (`OPENCODE_CONFIG`, `oc_config.build`). Процесс в своей группе (`start_new_session`): `stop()` гасит
группу SIGTERM -> SIGKILL и добивает по `/proc` потомков, ушедших из группы (F18: у 1.18.31 сирот нет, это
страховка). `opencode serve` владельца на 4096 этот модуль не трогает никогда: порт всегда свой, случайный.

Окружение сервера собирает вызывающий (белый список демона); `start` дополнительно выкидывает всё
`ANTHROPIC_*`/`CLAUDE*` (ключи и настройки Claude сессии OpenCode не нужны) и чужие `OPENCODE_*` (например,
`OPENCODE_PERMISSION` или `OPENCODE_CONFIG_CONTENT` родителя переписали бы наши права поверх конфига, F2),
оставляя из `OPENCODE_*` только осознанные выключатели `KEEP_OPENCODE_ENV`. `OPENCODE_CONFIG_DIR` ставит
только сам хаб (`OcServer(config_dir=…)`, Ф71-3): OpenCode берёт из этого каталога глобальный `AGENTS.md`
вместо `~/.config/opencode/AGENTS.md` (сам глобальный конфиг и плагины владельца по-прежнему грузятся — проба
`.runs/f713-probe/`).

`OcClient` — `httpx.AsyncClient` с basic auth; `directory` уходит в query КАЖДОГО запроса (F3). `always` в
ответ на разрешение клиент не отправит никогда (F20, П10).
"""

import asyncio
import contextlib
import json
import logging
import os
import secrets
import signal
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx

from hub.agent.files import private_dir
from hub.work.session import descendants

from . import oc_config

log = logging.getLogger(__name__)

USER = "opencode"
READY_TIMEOUT_S = 20.0
SUMMARIZE_TIMEOUT_S = 600.0  # Ф41-4б: сжатие разговора — запрос может идти до конца сжатия
LOG_TAIL_BYTES = 4000
KEEP_OPENCODE_ENV = frozenset(
    {"OPENCODE_DISABLE_CLAUDE_CODE", "OPENCODE_DISABLE_PROJECT_CONFIG", "OPENCODE_PURE"}
)
DROP_PREFIXES = ("ANTHROPIC_", "CLAUDE")
RECONNECT_MIN_S = 0.5
RECONNECT_MAX_S = 5.0
# Событие, которое `events()` вставляет после переподключения потока (не событие OpenCode): драйвер хода по
# нему сразу сверяет состояние сессии — пока потока не было, `session.idle` мог потеряться.
RECONNECTED = "hub.reconnected"
# Предупреждение «сохранённые разрешения (always)» — не чаще раза в сутки на папку (в памяти процесса хоста):
# приватный сервер стартует на каждый подъём сессии, строка шла сотнями.
SAVED_WARN_EVERY_S = 86400.0
_saved_warned: dict[str, float] = {}


def _saved_rules(data) -> bool:
    """Есть ли сохранённые разрешения в ответе `GET /api/permission/saved`: 1.18 — список, 1.19 —
    `{"data": [...]}` (проба Ф99-0, 28.10; пустой словарь-обёртка — не повод предупреждать)."""
    if isinstance(data, dict):
        data = data.get("data")
    return bool(data)


def _saved_warn_due(cwd: str, now: float | None = None) -> bool:
    """`True` — про сохранённые разрешения папки `cwd` пора написать (и отметка ставится)."""
    now = time.monotonic() if now is None else now
    last = _saved_warned.get(cwd)
    if last is not None and now - last < SAVED_WARN_EVERY_S:
        return False
    _saved_warned[cwd] = now
    return True


class EngineStartError(Exception):
    """Сервер не поднялся. Текст — причина и хвост журнала сервера (пароль в журнал не пишется)."""


class _SealedOpen(Exception):
    """Агент, запертый нашим конфигом, на сервере открыт (имена — в тексте)."""


class SessionNotFound(Exception):
    """404 на запрос к сессии (сессии нет у этого сервера/каталога)."""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def log_path(data_dir: Path, role: str, ident: str | int) -> Path:
    """`<data_dir>/engine/logs/<роль>-<id>.log` (каталоги 0700)."""
    return private_dir(Path(data_dir) / "engine" / "logs") / f"{role}-{ident}.log"


def server_env(env: dict, *, config_path: Path, password: str, config_dir: Path | None = None) -> dict:
    """Окружение процесса `opencode serve` (см. докстринг модуля)."""
    result = {}
    for key, value in env.items():
        if key.startswith(DROP_PREFIXES):
            continue
        if key.startswith("OPENCODE") and key not in KEEP_OPENCODE_ENV:
            continue
        result[key] = value
    result["OPENCODE_CONFIG"] = str(config_path)
    result["OPENCODE_SERVER_PASSWORD"] = password
    result["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
    # Ф86-1 (П8): сервер OpenCode хаба — плагин хаба в нём не подкладывает сводку при сжатии (хаб подаёт сам).
    result["HUB_OC_SERVER"] = "1"
    if config_dir is not None:
        result["OPENCODE_CONFIG_DIR"] = str(config_dir)
    return result


def kill_tree(pid: int) -> None:
    """SIGKILL процессу и всем его потомкам по `/proc` (у `work.session.Process` — свой метод)."""
    for p in [*descendants(pid), pid]:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(p, signal.SIGKILL)


def _tail(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - LOG_TAIL_BYTES))
            return f.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


class OcServer:
    """Один приватный `opencode serve`. `binary` — путь из `choice.opencode_path` (в тестах — поддельный)."""

    def __init__(self, binary: str, *, log_file: Path, config_dir: Path | None = None) -> None:
        self.binary = binary
        self.log_file = Path(log_file)
        self.config_dir = config_dir
        self.port: int | None = None
        self.password: str | None = None
        self.proc: subprocess.Popen | None = None
        self.cwd: str | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def auth(self) -> tuple[str, str]:
        assert self.password is not None
        return (USER, self.password)

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc is not None else None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def client(self, directory: str | None = None, **kwargs) -> OcClient:
        return OcClient(self.base_url, self.auth, directory or self.cwd or "/", alive=self.alive, **kwargs)

    async def start(
        self, cwd: str, env: dict, config_path: Path, *, ready_timeout_s: float = READY_TIMEOUT_S
    ) -> None:
        """Запустить и дождаться готовности (`GET /agent` 200). Не поднялся — сервер погашен,
        `EngineStartError` с хвостом журнала."""
        if self.proc is not None:
            raise RuntimeError("сервер уже запущен")
        self.cwd = cwd
        self.port = free_port()
        self.password = secrets.token_hex(16)
        private_dir(self.log_file.parent)
        full_env = server_env(
            env, config_path=config_path, password=self.password, config_dir=self.config_dir
        )
        argv = [self.binary, "serve", "--hostname", "127.0.0.1", "--port", str(self.port)]
        self.proc = self._spawn(argv, cwd, full_env)
        deadline = time.monotonic() + ready_timeout_s
        async with httpx.AsyncClient(base_url=self.base_url, auth=self.auth, timeout=2.0) as http:
            while True:
                if self.proc.poll() is not None:
                    rc = self.proc.returncode
                    await self.stop(grace_s=0.5)
                    raise EngineStartError(f"opencode serve вышел с кодом {rc}: {_tail(self.log_file)}")
                with contextlib.suppress(httpx.HTTPError):
                    r = await http.get("/agent", params={"directory": cwd})
                    if r.status_code == 200:
                        break
                if time.monotonic() >= deadline:
                    await self.stop(grace_s=0.5)
                    raise EngineStartError(
                        f"opencode serve не ответил за {ready_timeout_s:g} с: {_tail(self.log_file)}"
                    )
                await asyncio.sleep(0.1)
            # П10: `always` хаб не отвечает, но сохранённые разрешения в данных OpenCode (общих с владельцем) могли
            # остаться — OpenCode перестал бы спрашивать. Только предупреждение.
            with contextlib.suppress(httpx.HTTPError, ValueError):
                r = await http.get("/api/permission/saved", params={"directory": cwd})
                if r.status_code == 200 and _saved_rules(r.json()) and _saved_warn_due(cwd):
                    log.warning(
                        "OpenCode: есть сохранённые разрешения (always) для %s — гард их не увидит", cwd
                    )

    async def start_locked(
        self, cwd: str, env: dict, config_path: Path, *, ready_timeout_s: float = READY_TIMEOUT_S
    ) -> list:
        """`start` + запирание чужих агентов (ревью Ф16): агенты проекта и владельца (`.opencode/agents/*.md`,
        `mode: all|subagent`) приходят на приватный сервер с правами OpenCode по умолчанию `"*": "allow"` —
        их вызовы не дают `permission.asked`, гард хаба их не видит. Такие агенты выключаются в конфиге
        (`oc_config.lock_agents`), сервер перезапускается один раз; остались — `EngineStartError`, сервер
        погашен. Возвращает `GET /agent` итогового сервера."""
        await self.start(cwd, env, config_path, ready_timeout_s=ready_timeout_s)
        try:
            config = json.loads(Path(config_path).read_text(encoding="utf-8"))  # noqa: ASYNC240 — свой файл 0600
            async with self.client(cwd) as client:
                agents = await client.agents()
            names = oc_config.unlocked_agents(agents, config)
            if not names:
                return agents
            # Ревью 41-4 (4): агент, который наш конфиг держит запертым, на сервере открыт — выключать его
            # нельзя (он нужен движку), работать с открытым — тоже: запуск отменяется.
            open_sealed = sorted(set(names) & set(oc_config.sealed_agents(config)))
            if open_sealed:
                raise _SealedOpen(", ".join(open_sealed))
            log.info("OpenCode: выключаю агентов без запросов разрешений: %s", ", ".join(names))
            config = oc_config.lock_agents(config, names)
            oc_config.write(Path(config_path), config)
            await self.stop(grace_s=1.0)
            self.proc = None
            await self.start(cwd, env, config_path, ready_timeout_s=ready_timeout_s)
            async with self.client(cwd) as client:
                agents = await client.agents()
            left = oc_config.unlocked_agents(agents, config)
        except _SealedOpen as exc:
            await self.stop(grace_s=0.5)
            raise EngineStartError(f"агент OpenCode не заперт на сервере: {exc} — запуск отменён") from None
        except (httpx.HTTPError, OSError, ValueError) as exc:
            await self.stop(grace_s=0.5)
            why = exc.__class__.__name__
            raise EngineStartError(f"права агентов OpenCode не проверены ({why})") from None
        if left:
            await self.stop(grace_s=0.5)
            names_left = ", ".join(left)
            raise EngineStartError(f"агенты OpenCode без запросов разрешений не выключились: {names_left}")
        return agents

    def _spawn(self, argv: list[str], cwd: str, env: dict) -> subprocess.Popen:
        """Процесс сервера в своей группе, вывод — в журнал (0600)."""
        with open(self.log_file, "ab") as out:
            os.chmod(self.log_file, 0o600)
            try:
                return subprocess.Popen(  # noqa: S603 — без shell, бинарь из engine.json/which
                    argv,
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as exc:
                raise EngineStartError(f"opencode не запустился: {exc.strerror or exc}") from None

    async def stop(self, grace_s: float = 5.0) -> None:
        """SIGTERM группе, ожидание `grace_s`, SIGKILL группе, добивание потомков по `/proc`. Повторный
        вызов — без эффекта."""
        proc = self.proc
        if proc is None:
            return
        tree = descendants(proc.pid)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGTERM)
        for _ in range(max(1, int(grace_s / 0.05))):
            if proc.poll() is not None:
                break
            await asyncio.sleep(0.05)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        if proc.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                proc.kill()
        # Потомки, ушедшие в свою группу/сессию, killpg не достал.
        for p in tree:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(p, signal.SIGKILL)
        for _ in range(100):
            if proc.poll() is not None:
                break
            await asyncio.sleep(0.02)


class OcClient:
    """Клиент HTTP API сервера OpenCode (v1, F3). `directory` — абсолютный каталог сессий этого клиента."""

    def __init__(
        self,
        base_url: str,
        auth: tuple[str, str],
        directory: str,
        *,
        timeout: float = 30.0,
        alive: Callable[[], bool] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.directory = directory
        self._alive = alive or (lambda: True)
        self._http = httpx.AsyncClient(base_url=base_url, auth=auth, timeout=timeout, transport=transport)
        self._sse_auth = auth
        self._base_url = base_url
        self._transport = transport

    async def __aenter__(self) -> OcClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    def _q(self, extra: dict | None = None) -> dict:
        params = {"directory": self.directory}
        if extra:
            params.update(extra)
        return params

    async def _request(self, method: str, url: str, *, json_body=None, params: dict | None = None):
        r = await self._http.request(method, url, params=self._q(params), json=json_body)
        if r.status_code == 404 and url.startswith("/session/ses"):
            raise SessionNotFound(url)
        r.raise_for_status()
        return r

    # --- сессии --------------------------------------------------------------------------------------------

    async def create_session(
        self, title: str, agent: str | None = None, parent_id: str | None = None
    ) -> dict:
        body: dict = {"title": title}
        if agent:
            body["agent"] = agent
        if parent_id:
            body["parentID"] = parent_id
        return (await self._request("POST", "/session", json_body=body)).json()

    async def delete_session(self, sid: str) -> None:
        """Удалить сессию из данных OpenCode (Ф41-2б: разговор, созданный взамен пропавшего, не остаётся в
        общем списке сессий владельца)."""
        await self._request("DELETE", f"/session/{sid}")

    async def get_session(self, sid: str) -> dict | None:
        try:
            return (await self._request("GET", f"/session/{sid}")).json()
        except SessionNotFound:
            return None

    async def prompt(
        self,
        sid: str,
        *,
        text: str,
        system: str | None = None,
        agent: str | None = None,
        model: tuple[str, str] | None = None,
        variant: str | None = None,
        files: list[dict] | None = None,
        synthetic: bool = False,
        texts: list[tuple[str, bool]] | None = None,
    ) -> None:
        """`prompt_async` (204 сразу; в занятую сессию — в очередь хода, F10). 404 -> `SessionNotFound`.
        Ф41-3в: `files` — части `{"type": "file", "mime", "filename", "url"}` после текста; пустой текст при
        файлах не шлётся. Ф86-1 (П2): `texts` — текстовые части по одной `(текст, синтетическая)` вместо
        `text` (справка хаба отдельной синтетической частью, реплика владельца — обычной)."""
        if texts is not None:
            parts = [
                {"type": "text", "text": body, **({"synthetic": True} if flag or synthetic else {})}
                for body, flag in texts
                if body
            ]
        else:
            parts = [{"type": "text", "text": text}] if text or not files else []
        if synthetic:
            # Ф41-4 (3.1): сигнал хаба — часть `synthetic: true` (в выгрузке разговора отличима от реплики
            # владельца; модель её видит).
            for part in parts:
                part["synthetic"] = True
        body: dict = {"parts": [*parts, *(files or [])]}
        if agent:
            body["agent"] = agent
        if model:
            body["model"] = {"providerID": model[0], "modelID": model[1]}
        if variant:
            body["variant"] = variant
        if system:
            body["system"] = system
        await self._request("POST", f"/session/{sid}/prompt_async", json_body=body)

    async def status_all(self) -> dict:
        data = (await self._request("GET", "/session/status")).json()
        return data if isinstance(data, dict) else {}

    async def status_info(self, sid: str) -> dict:
        """Статус сессии; нет в словаре — простаивает (F3)."""
        info = (await self.status_all()).get(sid)
        return info if isinstance(info, dict) and info.get("type") else {"type": "idle"}

    async def status(self, sid: str) -> str:
        return str((await self.status_info(sid)).get("type"))

    async def messages(self, sid: str) -> list:
        data = (await self._request("GET", f"/session/{sid}/message")).json()
        return data if isinstance(data, list) else []

    async def message(self, sid: str, mid: str) -> dict:
        return (await self._request("GET", f"/session/{sid}/message/{mid}")).json()

    async def abort(self, sid: str) -> None:
        await self._request("POST", f"/session/{sid}/abort")

    async def summarize(self, sid: str, model: tuple[str, str]) -> None:
        """Ф41-4б: сжатие разговора (`POST /session/{id}/summarize`); конец — событие `session.compacted`."""
        body = {"providerID": model[0], "modelID": model[1]}
        # Сервер может держать ответ до конца сжатия — свой срок запроса (общий — 30 с).
        r = await self._http.post(
            f"/session/{sid}/summarize", params=self._q(), json=body, timeout=SUMMARIZE_TIMEOUT_S
        )
        if r.status_code == 404:
            raise SessionNotFound(f"/session/{sid}/summarize")
        r.raise_for_status()

    # --- разрешения и вопросы ------------------------------------------------------------------------------

    async def reply_permission(self, request_id: str, reply: str, message: str | None = None) -> None:
        """`once` или `reject` (+ `message` — причина: модель её увидит и продолжит ход, F8). `always` —
        никогда (F20, П10)."""
        if reply not in ("once", "reject"):
            raise ValueError(f"ответ разрешения {reply!r} не допускается (только once/reject)")
        body: dict = {"reply": reply}
        if message:
            body["message"] = message
        await self._request("POST", f"/permission/{request_id}/reply", json_body=body)

    async def reply_question(self, request_id: str, answers: list[list[str]]) -> None:
        await self._request("POST", f"/question/{request_id}/reply", json_body={"answers": answers})

    async def reject_question(self, request_id: str) -> None:
        await self._request("POST", f"/question/{request_id}/reject")

    async def pending_permissions(self) -> list:
        """Запасной путь после переподключения: `GET /permission`. 400 (F9: ломается на запросах `grep`) и
        прочие сбои — пустой список, источник запросов — поток событий."""
        try:
            r = await self._http.get("/permission", params=self._q())
        except httpx.HTTPError:
            return []
        if r.status_code != 200:
            return []
        try:
            data = r.json()
        except ValueError:
            return []
        return data if isinstance(data, list) else []

    async def agents(self) -> list:
        data = (await self._request("GET", "/agent")).json()
        return data if isinstance(data, list) else []

    async def mcp_status(self) -> dict:
        data = (await self._request("GET", "/mcp")).json()
        return data if isinstance(data, dict) else {}

    async def model_info(self, provider: str, model: str) -> dict | None:
        """Ф41-3 (2.3): предел контекста и приём картинок модели из `GET /config/providers` —
        `{"context": int | None, "image": bool | None}`. ВНИМАНИЕ: ответ сервера несёт ключ провайдера (поле
        `key`) — наружу отдаются ТОЛЬКО `limit.context` и `capabilities.input.image`; сам ответ не
        возвращается, не журналируется и в события не пишется. Модели нет — `None`."""
        data = (await self._request("GET", "/config/providers")).json()
        providers = data.get("providers") if isinstance(data, dict) else data
        if isinstance(providers, dict):
            providers = [{"id": key, **value} for key, value in providers.items() if isinstance(value, dict)]
        for item in providers if isinstance(providers, list) else []:
            if not isinstance(item, dict) or item.get("id") != provider:
                continue
            models = item.get("models")
            entry = models.get(model) if isinstance(models, dict) else None
            if not isinstance(entry, dict):
                return None
            limit = entry.get("limit") if isinstance(entry.get("limit"), dict) else {}
            context = limit.get("context")
            if isinstance(context, bool) or not isinstance(context, int | float) or context < 0:
                context = None
            capabilities = entry.get("capabilities") if isinstance(entry.get("capabilities"), dict) else {}
            accepts = capabilities.get("input") if isinstance(capabilities.get("input"), dict) else {}
            image = accepts.get("image")
            return {
                "context": None if context is None else int(context),
                "image": image if isinstance(image, bool) else None,
            }
        return None

    async def model_list(self) -> list[dict]:
        """Ф75-1: модели этого сервера из `GET /config/providers` — `[{id: "провайдер/модель", name, images,
        variants}]`, по порядку провайдеров. Как у `model_info`: ответ несёт ключи провайдеров — наружу идут
        ТОЛЬКО имя модели, приём картинок (`capabilities.input.image`) и имена уровней (`variants`)."""
        data = (await self._request("GET", "/config/providers")).json()
        providers = data.get("providers") if isinstance(data, dict) else data
        if isinstance(providers, dict):
            providers = [{"id": key, **value} for key, value in providers.items() if isinstance(value, dict)]
        found: list[dict] = []
        for item in providers if isinstance(providers, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            models = item.get("models")
            for model_id, entry in models.items() if isinstance(models, dict) else []:
                if not isinstance(model_id, str) or not isinstance(entry, dict):
                    continue
                capabilities = (
                    entry.get("capabilities") if isinstance(entry.get("capabilities"), dict) else {}
                )
                accepts = capabilities.get("input") if isinstance(capabilities.get("input"), dict) else {}
                image = accepts.get("image")
                variants = entry.get("variants")
                name = entry.get("name")
                found.append(
                    {
                        "id": f"{item['id']}/{model_id}",
                        "name": name if isinstance(name, str) else None,
                        "images": image if isinstance(image, bool) else None,
                        "variants": [v for v in variants if isinstance(v, str)]
                        if isinstance(variants, dict)
                        else [],
                    }
                )
        return found

    # --- события -------------------------------------------------------------------------------------------

    async def events(self) -> AsyncIterator[dict]:
        """Поток SSE `GET /event` (F6). Обрыв при живом сервере — переподключение с паузой 0,5–5 с; после
        переподключения — событие `RECONNECTED` и запасной `GET /permission` (как `permission.asked`, дубли
        отсеивает драйвер). Сервер погашен — поток кончается."""
        pause = RECONNECT_MIN_S
        first = True
        timeout = httpx.Timeout(10.0, read=None)
        async with httpx.AsyncClient(
            base_url=self._base_url, auth=self._sse_auth, timeout=timeout, transport=self._transport
        ) as http:
            while True:
                connected = False
                try:
                    async with http.stream("GET", "/event", params=self._q()) as r:
                        if r.status_code == 200:
                            connected = True
                            pause = RECONNECT_MIN_S
                            if not first:
                                yield {"type": RECONNECTED, "properties": {}}
                                for req in await self.pending_permissions():
                                    if isinstance(req, dict):
                                        yield {"type": "permission.asked", "properties": req}
                            first = False
                            async for line in r.aiter_lines():
                                if not line.startswith("data:"):
                                    continue
                                try:
                                    event = json.loads(line[5:].strip())
                                except ValueError:
                                    continue
                                if isinstance(event, dict):
                                    yield event
                except httpx.HTTPError:
                    pass
                if not self._alive():
                    return
                if not connected:
                    pause = min(RECONNECT_MAX_S, pause * 2)
                await asyncio.sleep(pause)
