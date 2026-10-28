"""Хост сессий хаба `hub work run` (Ф41-2б; задачи по «Го» Ф9-1 убраны в Ф78-1).

`WorkHost` — общая часть хоста: длинное ожидание ядра (`/work/host/wait`), движок сессий, паузы лимита
подписки, проверка входа движков, уведомления, проекты, состояние для `hub work status`. Постоянные сессии
хаба — `held.HeldSessions` (свои реестры, раннеры и буферы событий).

Журнал процесса — без текстов: id сессий и проектов, виды событий и отказов."""

import asyncio
import contextlib
import logging
import os
import time
import traceback
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from hub.agent.core_api import CoreError, CoreUnauthorized, CoreUnavailable
from hub.agent.dispatcher import escape_markup, in_window
from hub.core import engine as core_engine
from hub.engine import failure
from hub.engine import registry as engine_registry
from hub.mcp_server import format as fmt

from . import files, session
from .held import HeldSessions

_logger = logging.getLogger("hub.work")

__all__ = ["EXIT_UNAUTHORIZED", "WorkHost"]

EXIT_UNAUTHORIZED = 78
AUTH_CHECK_S = 600
AUTH_RECHECK_FAILED_S = 30
HELD_ROUND_MAX_S = 25.0
PAUSE_MIN_S = 1800
OVERAGE_PAUSE_S = 3600
SHUTDOWN_GRACE_S = 20.0
PROJECTS_REFRESH_S = 60.0

Notifier = Callable[[str, str, str], None]
AuthChecker = Callable[[str, dict], tuple[bool, str]]


class WorkHost:
    def __init__(
        self,
        *,
        core,
        config: dict,
        config_dir: Path,
        data_dir: Path,
        notifier: Notifier,
        environ: dict | None = None,
        auth_checker: AuthChecker | None = None,
        home: Path | None = None,
        guard_path: str | None = None,
        wall: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.core = core
        self.config = config
        self.config_dir = config_dir
        self.data_dir = data_dir
        self._notifier = notifier
        self.environ = dict(os.environ if environ is None else environ)
        self._auth_checker = auth_checker
        self.home = Path.home() if home is None else home
        self._guard_path = guard_path
        self.wall = wall
        self.monotonic = monotonic

        self.projects: dict[int, dict] = {}
        self.since = 0
        self.state: dict = {"counters": {}}
        self._projects_at: float | None = None
        # Ф41-2б П6: момент последней проверки входа — на движок (сессии хаба идут каждая своим движком).
        self._auth_ats: dict[str, float] = {}
        self._kick = asyncio.Event()
        self._flush_now = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        self.stopping = False
        # Ф16 (3.2, П11): движок сессий хаба — из `/work/host/open` на старте и из каждого `/work/host/wait`.
        self.engine = "claude"
        # Ф66-3 F: последняя отметка «паузу лимита снял владелец», о которой хост знает (мс часов ядра); уходит в
        # `?pause_cleared=` длинного ожидания — отличие от отметки ядра будит ответ.
        self.pause_cleared_ts = 0
        # Ф41-2б: постоянные сессии хаба — свои реестры, раннеры и потолки.
        self.held = HeldSessions(self)

    # --- мелочи ---------------------------------------------------------------------------------------------

    def _now_ms(self) -> int:
        return int(self.wall() * 1000)

    def _count(self, key: str, n: int = 1) -> None:
        counters = self.state.setdefault("counters", {})
        counters[key] = counters.get(key, 0) + n

    def count(self, key: str, n: int = 1) -> None:
        self._count(key, n)

    def kick(self) -> None:
        self._kick.set()

    def flush_soon(self) -> None:
        self._flush_now.set()

    def spawn(self, coro) -> asyncio.Task:
        """Фоновая задача хоста со ссылкой (иначе сборщик мусора может снять её посреди работы)."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def guard_command(self, workdir: str, free_args=(), home_dir=None) -> str:
        """9a-a: лёгкая точка входа `hub-work-guard` рядом с интерпретатором; нет — `python -m
        hub.work.guard`. Ф50-3: `free_args` — аргументы про сокет набора снятых выключателей
        автопилота (`free_sock.FreeServer.args`): хук спрашивает набор у хоста на каждый вызов, в командной
        строке его нет. Ф50-7 (Р6): `home_dir` — папка
        процесса сессии; пишется (`--home-dir`), только когда она не папка проекта."""
        import shlex
        import shutil
        import sys

        if self._guard_path:
            head = [self._guard_path]
        else:
            candidate = Path(os.path.abspath(sys.executable)).parent / "hub-work-guard"
            if candidate.exists():
                head = [str(candidate)]
            else:
                found = shutil.which("hub-work-guard")
                head = [os.path.abspath(found)] if found else [sys.executable, "-m", "hub.work.guard"]
        tail = list(free_args)
        if home_dir and str(home_dir) != workdir:
            tail += ["--home-dir", str(home_dir)]
        return shlex.join(
            [*head, "--workdir", workdir, "--log", str(files.guard_log_path(self.data_dir)), *tail]
        )

    def cursor_hook_command(self) -> list[str]:
        """Ф71-1: хук Cursor — `hub-cursor-hook` рядом с интерпретатором хоста, иначе в PATH, иначе
        `python -m hub.work.cursor_hook` (тонкий клиент сокета хоста, только stdlib)."""
        import shutil
        import sys

        candidate = Path(os.path.abspath(sys.executable)).parent / "hub-cursor-hook"
        if candidate.exists():
            return [str(candidate)]
        found = shutil.which("hub-cursor-hook")
        if found:
            return [os.path.abspath(found)]
        return [sys.executable, "-m", "hub.work.cursor_hook"]

    def codex_hook_command(self) -> list[str]:
        """Ф74-5: хук Codex — `hub-codex-hook` рядом с интерпретатором хоста, иначе в PATH, иначе
        `python -m hub.work.codex_hook` (тот же тонкий клиент сокета хоста, только stdlib)."""
        import shutil
        import sys

        candidate = Path(os.path.abspath(sys.executable)).parent / "hub-codex-hook"
        if candidate.exists():
            return [str(candidate)]
        found = shutil.which("hub-codex-hook")
        if found:
            return [os.path.abspath(found)]
        return [sys.executable, "-m", "hub.work.codex_hook"]

    def rules_warnings(self, project_id: int, warnings: list[str]) -> None:
        """9a-a: правила не теряются молча — журнал и `hub work status`."""
        for warning in warnings:
            _logger.warning("work: проект %d — правила сессии: %s", project_id, warning)
        by_project = self.state.setdefault("rules_warnings", {})
        if warnings:
            by_project[str(project_id)] = warnings
        else:
            by_project.pop(str(project_id), None)

    def _quiet(self) -> bool:
        moment = datetime.fromtimestamp(self.wall())
        return in_window(self.config.get("quiet_hours"), moment.hour * 60 + moment.minute)

    def notify(self, title: str, text: str, urgency: str = "normal") -> None:
        if self._quiet():
            self._count("notify_quiet")
            return
        try:
            self._notifier(fmt.one_line(title), escape_markup(fmt.one_line(text)), urgency)
        except Exception as exc:  # noqa: BLE001 — уведомление не роняет хост
            _logger.warning("work: уведомление не отправлено (%s)", exc.__class__.__name__)

    def save(self) -> None:
        self.state["last_loop_ts"] = self._now_ms()
        self.state["engine"] = self.engine
        self.state["held"] = self.held.status()  # Ф41-2б: живые процессы постоянных сессий
        try:
            files.save_state(self.data_dir, self.state)
        except OSError as exc:
            _logger.warning("work: state.json не записан (%s)", exc.__class__.__name__)

    # --- события --------------------------------------------------------------------------------------------

    async def flush(self) -> None:
        """F4: накопленные события сессий хаба — в ядро (буферы — у `HeldSessions`)."""
        await self.held.flush()

    async def _flush_loop(self, stop: asyncio.Event) -> None:
        period = float(self.config.get("batch_s", 1.0))
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._flush_now.wait(), period)
            self._flush_now.clear()
            try:
                await self.flush()
            except CoreUnauthorized:
                return
            except Exception as exc:  # noqa: BLE001 — насос событий не должен падать
                _logger.warning("work: сбой отправки событий (%s)", exc.__class__.__name__)

    # --- лимит подписки (П7) --------------------------------------------------------------------------------

    def on_rate_limit(self, info) -> None:
        if not isinstance(info, dict):
            return
        # Ф16 П2, П12: событие OpenCode (`OcProcess`) несёт `engine` и свой текст причины (лимит/баланс или
        # ключ провайдера); пауза пишется с движком и держит только его. Ф74-5: так же у Codex
        # (`CodexProcess`); Ф74-8: любой движок сессий хаба из профиля (Cursor — `CursorProcess`), событие без
        # движка или с незнакомым — Claude Code.
        named = info.get("engine")
        engine = named if engine_registry.held(named) is not None else "claude"
        if info.get("status") == "rejected":
            reset = info.get("resetsAt")
            now = self.wall()
            until = float(reset) + 60 if isinstance(reset, int | float) and reset > now else now + PAUSE_MIN_S
            by_engine = self.state.setdefault("pauses", {})
            # Пауза OpenCode — на провайдера модели (`opencode:<провайдер>`): отказ ключа anthropic не держит
            # сессии на deepseek. Движки без провайдера — на имя движка, как было.
            provider = info.get("provider")
            key = failure.pause_key(engine, provider if isinstance(provider, str) else None)
            prev = by_engine.get(key)
            already = isinstance(prev, dict) and prev.get("until", 0) > self._now_ms()
            if already:
                # Действующая пауза не укорачивается: Codex шлёт отказ с `resetsAt` и следом без него
                # (живой журнал 26.10: 13:11 -> 09:31) — срок берётся дальний.
                until = max(until, int(prev["until"]) / 1000)
            reason = str(info.get("reason") or "") if engine != "claude" else ""
            # Ф66-3 F: `since` — когда пришёл последний отказ; «Снять паузу лимита» действует на записи,
            # начатые раньше отметки владельца.
            # Ф41-2б П6: запись паузы — на движок (пауза движка А не затирает запись движка Б).
            # Ф83-1 (П12): причина паузы — отдельным полем `kind` (`failure.classify`), `reason` — текст для
            # людей, как был.
            by_engine[key] = {
                "until": int(until * 1000),
                "reason": reason or "лимит подписки",
                "since": self._now_ms(),
                "kind": failure.classify(engine, info),
            }
            self._count("pauses")
            _logger.warning(
                "work: лимит (%s) — сессии хаба на паузе до %s", key, datetime.fromtimestamp(until)
            )
            if not already:
                self.notify(
                    "Хаб: работа на паузе",
                    f"{reason or 'лимит подписки'} — до "
                    f"{datetime.fromtimestamp(until).strftime('%d.%m %H:%M')}, сессии хаба ждут",
                    "critical",
                )
        if engine != "claude":
            return  # переплата — только у подписки Claude
        overage = bool(info.get("isUsingOverage") or info.get("overageInUse"))
        if overage:
            # Снимается в `resetsAt` события (нет его — через час) или событием без переплаты (ревью 9a).
            reset = info.get("resetsAt")
            now = self.wall()
            until = float(reset) if isinstance(reset, int | float) and reset > now else now + OVERAGE_PAUSE_S
            fresh = not isinstance(self.state.get("overage"), dict)
            self.state["overage"] = {
                "until": int(until * 1000),
                "reason": "работа сверх подписки",
                "since": self._now_ms(),
            }
            if fresh:
                _logger.warning("work: работа идёт сверх подписки — сессии Claude Code на паузе")
                self.notify("Хаб: работа идёт сверх подписки", "сессии Claude Code на паузе", "critical")
        elif self.state.get("overage"):
            self.state.pop("overage", None)

    def apply_pause_cleared(self, ts) -> bool:
        """Ф66-3 F: отметка ядра «паузу лимита снял владелец». Новая (отличается от известной) — убрать из
        состояния паузы лимита (`pauses` всех движков) и «работу сверх подписки» (`overage`),
        начатые не позже отметки; сохранить, разбудить круг. Старая отметка (раньше начала паузы) ничего не
        снимает: пауза, поставленная после неё, остаётся. Возвращает `True`, если что-то снято."""
        if not isinstance(ts, int) or isinstance(ts, bool) or ts == self.pause_cleared_ts:
            return False
        self.pause_cleared_ts = ts

        def stale(entry) -> bool:
            return isinstance(entry, dict) and (entry.get("since") or 0) <= ts

        removed = False
        pauses = self.state.get("pauses")
        if isinstance(pauses, dict):
            for key in [k for k, v in pauses.items() if stale(v)]:
                pauses.pop(key, None)
                removed = True
        if stale(self.state.get("overage")):
            self.state.pop("overage", None)
            removed = True
        if removed:
            _logger.info("work: пауза лимита снята владельцем")
            self.save()
            self.kick()
        return removed

    async def _check_auth(self, engine: str) -> tuple[bool, str]:
        """Проверка входа движка: Claude — `claude auth status` (по подписке), OpenCode — `engine.json`,
        бинарь и `opencode --version`."""
        from hub.engine import claude_run as wake_claude
        from hub.engine import opencode_run as wake_opencode

        if engine == "opencode":
            return await asyncio.to_thread(
                wake_opencode.auth_status, self.config_dir, self.environ, self.home
            )
        if engine == "cursor":
            return await asyncio.to_thread(self._cursor_auth)
        if engine == "codex":
            return await asyncio.to_thread(self._codex_auth)
        path = files.claude_path(self.config)
        env = session.build_env(self.environ, claude=path, task_id=0)
        checker = self._auth_checker or wake_claude.auth_status
        return await asyncio.to_thread(checker, path, env)

    def _cursor_auth(self) -> tuple[bool, str]:
        """Ф71-1 (Р8): Cursor — путь к `cursor-agent` из `engine.json` (секция `cursor`) или
        `~/.local/bin/cursor-agent`, файл исполняемый. Вход в Cursor (`cursor-agent login`) делает владелец; его
        отказ виден ошибкой первого хода."""
        from hub.engine import choice

        try:
            path = choice.cursor_path(choice.load_cursor(self.config_dir), Path(self.home))
        except choice.EngineUnavailable as exc:
            return False, f"Cursor: {exc}"
        return True, f"cursor-agent: {path}"

    def _codex_auth(self) -> tuple[bool, str]:
        """Ф74-5: Codex — путь к `codex` из `engine.json` (секция `codex`) или `~/.local/bin/codex` и
        `codex login status` (код 0 — вход есть; сам вход делает владелец, `codex login`)."""
        import subprocess

        from hub.engine import choice

        try:
            path = choice.codex_path(choice.load_codex(self.config_dir), Path(self.home))
        except choice.EngineUnavailable as exc:
            return False, f"Codex: {exc}"
        env = {k: v for k, v in self.environ.items() if not k.startswith("HUB_")}
        try:
            done = subprocess.run(  # noqa: S603 — свой бинарь из настроек
                [path, "login", "status"], capture_output=True, text=True, timeout=30, env=env, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"Codex: вход не проверен ({exc.__class__.__name__})"
        if done.returncode != 0:
            return False, "Codex: нет входа — владельцу выполнить `codex login`"
        return True, f"codex: {path}"

    async def engine_auth(self, engine: str) -> tuple[bool, str]:
        """Ф41-2б П6: вход движка `engine` для постоянных сессий — запись и момент проверки на движок (движок
        хоста и движок сессии различаются штатно). Отказ перепроверяется чаще (владелец вошёл и пишет снова)."""
        now = self.monotonic()
        auths = self.state.setdefault("auths", {})
        record = auths.get(engine)
        at = self._auth_ats.get(engine)
        fresh_s = AUTH_CHECK_S if isinstance(record, dict) and record.get("ok") else AUTH_RECHECK_FAILED_S
        if at is None or now - at >= fresh_s or not isinstance(record, dict):
            ok, text = await self._check_auth(engine)
            self._auth_ats[engine] = now
            record = auths[engine] = {"ok": ok, "text": text, "ts": self._now_ms()}
            if not ok:
                _logger.error("work: %s не запускается (постоянные сессии)", engine)
        return bool(record.get("ok")), str(record.get("text") or "")

    # --- цикл -----------------------------------------------------------------------------------------------

    async def refresh_projects(self, force: bool = False) -> None:
        now = self.monotonic()
        if not force and self._projects_at is not None and now - self._projects_at < PROJECTS_REFRESH_S:
            return
        rows = await self.core.get("/projects")
        self.projects = {
            p["id"]: {"name": p.get("name"), "workdir": p.get("workdir")}
            for p in rows
            if isinstance(p.get("id"), int) and p["id"] >= 0
        }
        self._projects_at = now

    async def start(self) -> None:
        """Старт: сессии хаба — своей ручкой восстановления, затем снимок `/work/host/open` (движок, отметка
        «паузу снял владелец», незакрытые сессии)."""
        await self.held.start()  # П8: постоянные сессии — своей ручкой восстановления
        opened = await self.core.get("/work/host/open")
        self.since = int(opened.get("max_seq") or 0)
        if opened.get("engine") in core_engine.ENGINES:
            self.engine = opened["engine"]  # П11: движок — до первого круга
        self.apply_pause_cleared(opened.get("pause_cleared_ts"))  # Ф66-3 F
        await self.refresh_projects(force=True)
        self.held.load(opened.get("sessions"))
        self.state["started_ts"] = self._now_ms()
        self.state["pid"] = os.getpid()
        self.save()

    async def apply(self, data: dict) -> None:
        self.since = max(self.since, int(data.get("max_seq") or 0))
        engine = data.get("engine")
        # Ф78-2 П1: любой движок переключателя. Прежний фильтр «claude/opencode» оставлял хосту старый движок
        # при `codex`/`cursor` — ядро отвечало на `/work/host/wait` сразу (горячая петля и запись state.json).
        if engine in core_engine.ENGINES and engine != self.engine:
            await self.engine_changed(engine)
        self.apply_pause_cleared(data.get("pause_cleared_ts"))  # Ф66-3 F
        # Ф41-2б: постоянные сессии — записи, решения карточек, очереди реплик.
        await self.held.apply(data.get("sessions"))

    async def engine_changed(self, engine: str) -> None:
        """Ф16 3.2: переключатель движка сменился. Сессии хаба идут каждая своим движком — хост только
        запоминает новый и будит круг."""
        _logger.info("work: движок сессий %s -> %s", self.engine, engine)
        self.engine = engine
        self.kick()

    async def tick(self) -> None:
        await self.refresh_projects()
        try:
            await self.held.tick()
        except CoreError as exc:
            _logger.warning("work: постоянные сессии — ядро отказало (HTTP %d)", exc.status)
        except CoreUnauthorized, CoreUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 — ревью 2б: сбой круга сессий не останавливает хост
            _logger.error("work: постоянные сессии — сбой круга (%s)", exc.__class__.__name__)
        self.save()

    def wait_params(self, timeout_s: float) -> dict:
        """Параметры длинного ожидания ядра. Ф41-4 П2: `signals_known` — готовность сигналов постоянных
        сессий, о которой хост уже знает (ожидание будит только отличие от неё)."""
        return {
            "since": self.since,
            "timeout_s": timeout_s,
            "engine": self.engine,
            "pause_cleared": self.pause_cleared_ts,
            "signals_known": self.held.signals_known(),
            # Ф83-1 (П9): паузы движков — ядру тем же запросом (для «Вернуть на основной» и карточки паузы).
            "pauses": self.pauses_param(),
        }

    def live_pauses(self) -> dict[str, dict]:
        """Ф83-1 (П9): действующие паузы движков `{ключ: {until, kind, since}}` (мс); ключ — движок или
        `opencode:<провайдер>` (`failure.pause_key`)."""
        now = self._now_ms()
        out: dict[str, dict] = {}
        pauses = self.state.get("pauses")
        for engine, entry in (pauses.items() if isinstance(pauses, dict) else ()):
            until = entry.get("until") if isinstance(entry, dict) else None
            if isinstance(until, int) and not isinstance(until, bool) and until > now:
                kind = entry.get("kind")
                out[engine] = {
                    "until": until,
                    "kind": kind if kind in failure.KINDS else "other",  # запись старого хоста — не повод
                    "since": int(entry.get("since") or 0),
                }
        return out

    def pauses_param(self) -> str:
        import json

        return json.dumps(self.live_pauses(), sort_keys=True, separators=(",", ":"))

    async def _wait(self, stop: asyncio.Event) -> dict | None:
        timeout_s = float(self.config.get("wait_timeout_s", 25))
        if self.held.runners:
            # Ф41-2б П1: круг — не реже раза в 30 с, пока есть живые постоянные сессии (подтверждение ядру).
            timeout_s = min(timeout_s, HELD_ROUND_MAX_S)
        request = asyncio.ensure_future(self.core.get("/work/host/wait", params=self.wait_params(timeout_s)))
        stopper = asyncio.ensure_future(stop.wait())
        kicker = asyncio.ensure_future(self._kick.wait())
        try:
            done, _ = await asyncio.wait({request, stopper, kicker}, return_when="FIRST_COMPLETED")
        finally:
            for task in (stopper, kicker):
                task.cancel()
        if request in done:
            return request.result()
        request.cancel()
        with contextlib.suppress(BaseException):
            await request
        return None

    async def run(self, stop: asyncio.Event) -> int:
        backoff = 5.0
        while not stop.is_set():
            try:
                await self.start()
                break
            except CoreUnauthorized:
                _logger.error("work: ядро не принимает токен — hub work setup; выход 78")
                return EXIT_UNAUTHORIZED
            except CoreError as exc:
                _logger.error(
                    "work: ядро отказало на старте (HTTP %d) — hub work setup; выход 78", exc.status
                )
                return EXIT_UNAUTHORIZED
            except CoreUnavailable as exc:
                _logger.warning("work: ядро недоступно на старте (%s)", exc)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), backoff)
                backoff = min(backoff * 2, 120.0)
        flusher = asyncio.create_task(self._flush_loop(stop))
        delay = 1.0
        code = 0
        try:
            while not stop.is_set():
                try:
                    self._kick.clear()
                    await self.tick()
                    data = await self._wait(stop)
                    if data is not None:
                        await self.apply(data)
                    delay = 1.0
                except CoreUnauthorized:
                    _logger.error("work: ядро не принимает токен — hub work setup; выход 78")
                    code = EXIT_UNAUTHORIZED
                    break
                except CoreUnavailable as exc:
                    _logger.warning("work: ядро недоступно (%s) — пауза", exc)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), delay)
                    delay = min(delay * 2, 60.0)
                except Exception as exc:  # noqa: BLE001 — непредвиденное: журнал без текстов, цикл жив
                    frame = traceback.extract_tb(exc.__traceback__)[-1] if exc.__traceback__ else None
                    where = f"{frame.filename}:{frame.lineno}" if frame else "?"
                    _logger.error("work: непредвиденная ошибка %s в %s", exc.__class__.__name__, where)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), delay)
                    delay = min(delay * 2, 60.0)
        finally:
            await self.shutdown()
            flusher.cancel()
            with contextlib.suppress(BaseException):
                await flusher
        return code

    async def shutdown(self) -> None:
        """F6: остановка хоста — сессиям хаба закрыть ввод, ждать до 20 с, затем погасить оставшиеся."""
        self.stopping = True
        # Ф41-2б П5: постоянным сессиям — закрыть ввод и ждать в тот же срок.
        waits = await self.held.shutdown_begin()
        if waits:
            await asyncio.wait(waits, timeout=SHUTDOWN_GRACE_S)
        await self.held.shutdown_finish()
        # дать наблюдателям выхода отработать
        await asyncio.sleep(0)
        with contextlib.suppress(Exception):
            await self.flush()
        self.save()
