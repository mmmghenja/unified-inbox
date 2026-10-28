"""Разовый ход модели без инструментов на любом из четырёх движков (Ф78-2,
docs/architecture.md). Им пользуются служба фактов `hub-facts`
и мозг агента владельца (`agent-owner`).

    turn = await oneshot.run("codex", prompt=…, system=…, model=…, effort=…, images=[путь, …], resume=None,
                             workdir=ws.path, timeout_s=120)

- `claude` — `claude -p --input-format stream-json --output-format stream-json --verbose --tools ""
  --strict-mcp-config --mcp-config '{"mcpServers":{}}' --setting-sources "" --disable-slash-commands
  --system-prompt …` (+ `--effort`, `--resume`; `persist=False` — `--no-session-persistence`); вход — одна
  строка stream-json с картинками блоками `image` (base64); окружение — `claude_run.build_env`, итог —
  `claude_run.classify` (П7: пауза лимита — только у Claude).
- `codex` — `codex exec [resume <нить>] --json --skip-git-repo-check --ignore-user-config --ignore-rules
  --disable shell_tool … -m <модель> -c model_reasoning_effort=… -c developer_instructions=…
  --image=<файл> -`,
  промпт — stdin. Модель и усилие подаются на КАЖДОМ ходе (П6: `resume` их не помнит).
- `cursor` — `cursor-agent -p --mode ask --trust --output-format json --model <choice.cursor_model>
  --workspace <каталог> [--resume <чат>]`, промпт — stdin; инструменты выключить нельзя: в каталоге
  `.cursor/cli.json` запрещает оболочку, запись, MCP и сеть, читать разрешено только картинки хода (их
  копии в каталоге). Системный текст — блоком в начале первого хода (у Cursor своего ключа нет).
- `opencode` — одноразовый приватный `opencode serve` на ход (как `hub-facts` раньше), права `{"*": "deny"}`,
  картинки — частями `file` (data URL). Агент владельца держит свой тёплый сервер и этот путь не берёт.

Процесс — в своей группе: тайм-аут и отмена (`CancelledError`) гасят её целиком (`claude_run.execute`).
Неудачный, оборванный или отменённый ход не двигает разговор (П6): `Turn.session` неудачного хода — тот же
`resume`, что пришёл. Вход в CLI не проверяется заранее: ход упал — для `codex`/`cursor` спрашивается их
локальный `login status`; нет входа — `Turn.auth` и текст «<движок>: нет входа — войди в терминале на машине
ядра» (П8).

Рабочий каталог (П2) — `Workspace`: `$XDG_RUNTIME_DIR/hub-brain/<движок>-<случайное>/` (0700, свой на каждый
мозг и на каждый вызов фактов; НЕ под `~/.config/hub` и `~/.local/share/hub` — там хук Cursor отказывает в
чтении). `Workspace.close()` удаляет каталог и записи разговоров CLI этого каталога (П9, `forget`)."""

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import choice
from . import claude_run as claude

_logger = logging.getLogger("hub.engine.oneshot")

ENGINES = ("claude", "opencode", "codex", "cursor")
BRAIN_DIR = "hub-brain"
STATUS_TIMEOUT_S = 20.0
ERROR_TEXT_MAX = 300
IMAGE_MAX_BYTES = 20 * 1024 * 1024
IMAGE_MIMES = ("image/png", "image/jpeg", "image/gif", "image/webp")
CURSOR_SYSTEM_OPEN = "<system_rules>"
CURSOR_SYSTEM_CLOSE = "</system_rules>"
CURSOR_SYSTEM_HEAD = "Системные правила этого разговора. Действуют весь разговор."
CURSOR_DENY = ("Shell(*)", "Write(*)", "Mcp(*:*)", "WebFetch(*)")
CURSOR_IMG_DIR = "img"
CODEX_DISABLE = ("shell_tool", "unified_exec", "apps", "plugins", "hooks")
CLAUDE_EMPTY_MCP = '{"mcpServers":{}}'
# П8: признаки «нет входа» в тексте отказа Claude (широкие маркеры лимита — в `claude_run.classify`).
CLAUDE_AUTH_MARKERS = ("/login", "log in", "logged in", "not logged", "oauth", "api key", "authentication")


@dataclass
class Turn:
    """Итог разового хода (3.5).

    `ok` — ответ есть (`text`); `session` — продолжение для следующего `resume` (неудачный ход — прежнее,
    П6); `error` — текст для журнала и владельца; `failure` — короткая метка отказа (`timeout`, `auth`, `limit`,
    `exit N`, `garbage`, `start`, `model`, `cancelled`); `cost_usd` — только если движок его дал (claude,
    opencode); `tokens_in` — вход вместе с кэшем; `pause` — лимит подписки (вызывающий ждёт до `reset_ts`, не
    считает сбоем; у codex/cursor не определяется, П7); `auth` — нет входа в CLI на этой машине (П8)."""

    ok: bool
    text: str = ""
    session: str | None = None
    error: str | None = None
    cost_usd: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    duration_ms: int = 0
    pause: bool = False
    reset_ts: float | None = None
    auth: bool = False
    failure: str | None = None
    engine: str = ""
    model: str | None = None
    extra: dict = field(default_factory=dict)


def _fail(engine: str, failure: str, error: str, *, session: str | None, started: float, **kw) -> Turn:
    return Turn(
        ok=False,
        error=error[:ERROR_TEXT_MAX],
        failure=failure,
        session=session,
        engine=engine,
        duration_ms=int((time.monotonic() - started) * 1000),
        **kw,
    )


def _auth_text(engine: str) -> str:
    return f"{engine}: нет входа — войди в терминале на машине ядра"


# --- рабочий каталог (П2) и чистка записей CLI (П9) ---------------------------------------------------------


def runtime_root() -> Path:
    """`$XDG_RUNTIME_DIR/hub-brain` (память, только владелец); нет переменной — `<tmp>/hub-brain-<uid>`."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base):
        return Path(base) / BRAIN_DIR
    return Path(tempfile.gettempdir()) / f"{BRAIN_DIR}-{os.getuid()}"


def make_workdir(engine: str, root: Path | None = None) -> Path:
    """Новый пустой каталог `<root>/<движок>-<12 hex>` (0700)."""
    base = Path(root) if root is not None else runtime_root()
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = base / f"{engine}-{secrets.token_hex(6)}"
    path.mkdir(mode=0o700)
    return path


def claude_slug(workdir: Path | str) -> str:
    """Имя каталога записей Claude Code: путь, где всё, кроме латиницы и цифр, — «-» (проба 22.10)."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(workdir))


def cursor_chat_dir(workdir: Path | str) -> str:
    """Имя каталога чатов Cursor: md5 пути рабочего каталога (проба 22.10)."""
    return hashlib.md5(str(workdir).encode("utf-8")).hexdigest()  # noqa: S324 — имя каталога, не защита


def record_paths(
    engine: str, *, session: str | None, workdir: Path | str, home: Path | None = None
) -> list[Path]:
    """Записи разговоров CLI, принадлежащие ЭТОМУ рабочему каталогу и разговору (П9; пути — проба 22.10):

    - claude: `~/.claude/projects/<claude_slug(workdir)>/` целиком (каталог уникален — случайное имя);
    - codex: `~/.codex/sessions/<гггг>/<мм>/<дд>/rollout-<время>-<нить>.jsonl`;
    - cursor: `~/.cursor/chats/<md5(workdir)>/` и `~/.cursor/projects/<…имя каталога…>/`;
    - opencode: ничего (сессия удаляется через сервер в самом ходе)."""
    home = Path.home() if home is None else Path(home)
    workdir = Path(workdir)
    token = workdir.name
    out: list[Path] = []
    if engine == "claude":
        out.append(home / ".claude" / "projects" / claude_slug(workdir))
    elif engine == "codex" and session and re.fullmatch(r"[0-9A-Za-z-]{8,80}", session):
        out.extend(sorted((home / ".codex" / "sessions").glob(f"*/*/*/rollout-*-{session}.jsonl")))
    elif engine == "cursor":
        out.append(home / ".cursor" / "chats" / cursor_chat_dir(workdir))
        projects = home / ".cursor" / "projects"
        if re.fullmatch(r"[a-z]+-[0-9a-f]{12}", token) and projects.is_dir():
            out.extend(sorted(p for p in projects.iterdir() if p.is_dir() and p.name.endswith(token)))
    return [p for p in out if p.exists()]


def forget(engine: str, *, session: str | None, workdir: Path | str, home: Path | None = None) -> list[Path]:
    """Удалить записи разговора CLI (П9) -> удалённые пути. Ошибки удаления — в журнал, не исключение.

    Только для каталога из `make_workdir` (`<движок>-<12 hex>`): записи Claude Code и Cursor ищутся по пути
    каталога, и чужой путь (папка проекта) снёс бы записи настоящих сессий."""
    if not re.fullmatch(r"[a-z]+-[0-9a-f]{12}", Path(workdir).name):
        _logger.warning("oneshot: forget не для своего каталога — пропущено")
        return []
    removed: list[Path] = []
    for path in record_paths(engine, session=session, workdir=workdir, home=home):
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed.append(path)
        except OSError as exc:
            _logger.warning("oneshot: не удалил запись %s (%s)", path.name, exc.__class__.__name__)
    return removed


class Workspace:
    """Рабочий каталог мозга или вызова фактов (П2) и его чистка (П9).

        with oneshot.Workspace("cursor") as ws:
            turn = await ws.run(prompt=…, system=…, model=…, effort=…, images=…, resume=…, timeout_s=…)

    `run` — тот же `oneshot.run` с `workdir=ws.path`; разговоры запоминаются. `close()` (и выход из `with`)
    удаляет записи CLI каждого разговора и сам каталог. Повторный `close()` безвреден."""

    def __init__(self, engine: str, *, root: Path | None = None, home: Path | None = None) -> None:
        self.engine = engine
        self.home = home
        self.path = make_workdir(engine, root)
        self.sessions: set[str] = set()
        self.closed = False

    async def run(self, **kwargs) -> Turn:
        kwargs.setdefault("home", self.home)
        turn = await run(self.engine, workdir=self.path, **kwargs)
        if turn.session:
            self.sessions.add(turn.session)
        return turn

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for session in sorted(self.sessions) or [None]:
            forget(self.engine, session=session, workdir=self.path, home=self.home)
        shutil.rmtree(self.path, ignore_errors=True)

    def __enter__(self) -> Workspace:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# --- общее -------------------------------------------------------------------------------------------------


def _image_mime(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or ""
    if mime not in IMAGE_MIMES:
        raise ValueError(f"картинка {path.name}: нужен png, jpeg, gif или webp")
    return mime


def _images(images: Iterable[Path | str]) -> list[Path]:
    out = []
    for item in images or ():
        path = Path(item)
        if not path.is_file():
            raise ValueError(f"картинка {path.name}: нет файла")
        if path.stat().st_size > IMAGE_MAX_BYTES:
            raise ValueError(f"картинка {path.name}: больше {IMAGE_MAX_BYTES // (1024 * 1024)} МиБ")
        _image_mime(path)
        out.append(path)
    return out


def _binary(engine: str, config_dir: Path | None, home: Path, binary: str | None) -> str:
    if binary:
        return binary
    if engine == "claude":
        return choice.claude_path(None, home)
    if engine == "codex":
        return choice.codex_path(choice.load_codex(config_dir) if config_dir is not None else {}, home)
    if engine == "cursor":
        return choice.cursor_path(choice.load_cursor(config_dir) if config_dir is not None else {}, home)
    return choice.opencode_path(choice.load_overrides(config_dir) if config_dir is not None else {})


def _status_logged_in(argv: list[str], env: dict) -> bool | None:
    """Локальная проверка входа (`codex login status`, `cursor-agent status`): `True`/`False`, не вышло —
    `None`. Вывод не журналируется (там почта)."""
    try:
        proc = subprocess.run(  # noqa: S603 — путь к CLI, без shell
            argv,
            env=env,
            capture_output=True,
            text=True,
            timeout=STATUS_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    text = (proc.stdout or "") + (proc.stderr or "")
    if "not logged in" in text.lower():
        return False
    return proc.returncode == 0 and "logged in" in text.lower()


async def _logged_in(engine: str, binary: str, env: dict) -> bool | None:
    argv = [binary, "login", "status"] if engine == "codex" else [binary, "status"]
    return await asyncio.to_thread(_status_logged_in, argv, env)


def _int(value) -> int | None:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


# --- claude -------------------------------------------------------------------------------------------------


def claude_argv(
    *, binary: str, model: str, effort: str | None, system: str, resume: str | None, persist: bool
) -> list[str]:
    argv = [
        binary,
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        model,
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        CLAUDE_EMPTY_MCP,
        "--setting-sources",
        "",
        "--disable-slash-commands",
        "--system-prompt",
        system,
    ]
    if effort:
        argv += ["--effort", effort]
    if resume:
        argv += ["--resume", resume]
    if not persist:
        argv.append("--no-session-persistence")
    return argv


def claude_input(prompt: str, images: Sequence[Path]) -> str:
    content: list[dict] = []
    for path in images:
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        content.append(
            {"type": "image", "source": {"type": "base64", "media_type": _image_mime(path), "data": data}}
        )
    content.append({"type": "text", "text": prompt})
    line = {"type": "user", "message": {"role": "user", "content": content}}
    return json.dumps(line, ensure_ascii=False) + "\n"


async def _run_claude(
    *, binary, prompt, system, model, effort, images, resume, workdir, timeout_s, env, persist, started
) -> Turn:
    argv = claude_argv(
        binary=binary, model=model, effort=effort, system=system, resume=resume, persist=persist
    )
    try:
        raw = await claude.execute(
            argv,
            cwd=str(workdir),
            env=claude.build_env(env, binary),
            stdin_text=claude_input(prompt, images),
            timeout_s=timeout_s,
        )
    except OSError as exc:
        return _fail(
            "claude",
            "start",
            f"claude не запустился ({exc.__class__.__name__})",
            session=resume,
            started=started,
        )
    outcome = claude.classify(raw, time.time())
    data = outcome.data or {}
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    tokens_in = (
        sum(
            _int(usage.get(k)) or 0
            for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        )
        or None
    )
    common = {
        "engine": "claude",
        "model": model,
        "cost_usd": float(data["total_cost_usd"])
        if isinstance(data.get("total_cost_usd"), int | float)
        else None,
        "tokens_in": tokens_in,
        "tokens_out": _int(usage.get("output_tokens")),
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    if outcome.kind == "ok":
        sid = data.get("session_id") if isinstance(data.get("session_id"), str) else None
        return Turn(ok=True, text=outcome.result_text, session=sid or resume, **common)
    blob = (outcome.result_text + "\n" + raw.stderr).lower()
    if outcome.kind == "pause":
        if any(m in blob for m in CLAUDE_AUTH_MARKERS):
            return Turn(
                ok=False, session=resume, error=_auth_text("claude"), failure="auth", auth=True, **common
            )
        return Turn(
            ok=False,
            session=resume,
            error="claude: лимит подписки",
            failure="limit",
            pause=True,
            reset_ts=outcome.reset_ts,
            **common,
        )
    detail = outcome.result_text or raw.stderr.strip()
    text = f"claude: {outcome.failure}" + (f" — {detail[:200]}" if detail else "")
    return Turn(
        ok=False, session=resume, error=text[:ERROR_TEXT_MAX], failure=outcome.failure or "fail", **common
    )


# --- codex --------------------------------------------------------------------------------------------------


def _toml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # строка JSON — допустимая базовая строка TOML


def codex_argv(
    *,
    binary: str,
    model: str,
    effort: str | None,
    system: str,
    images: Sequence[Path],
    resume: str | None,
    persist: bool,
) -> list[str]:
    argv = [binary, "exec"]
    if resume:
        argv += ["resume"]
    argv += ["--json", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules"]
    for feature in CODEX_DISABLE:
        argv += ["--disable", feature]
    argv += ["-m", model]
    if effort:
        argv += ["-c", f"model_reasoning_effort={_toml_str(effort)}"]
    if system:
        argv += ["-c", f"developer_instructions={_toml_str(system)}"]
    if not persist:
        argv.append("--ephemeral")
    for path in images:
        argv.append(f"--image={path}")
    if resume:
        argv.append(resume)
    argv.append("-")
    return argv


def parse_codex(stdout: str) -> dict:
    """События `--json` -> `{thread, text, usage, errors}` (последнее сообщение агента — ответ)."""
    out: dict = {"thread": None, "text": None, "usage": None, "errors": [], "failed": False}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "thread.started" and isinstance(event.get("thread_id"), str):
            out["thread"] = event["thread_id"]
        elif kind == "item.completed" and isinstance(event.get("item"), dict):
            item = event["item"]
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                out["text"] = item["text"]
        elif kind == "turn.completed" and isinstance(event.get("usage"), dict):
            out["usage"] = event["usage"]
        elif kind in ("turn.failed", "error"):
            out["failed"] = True
            err = event.get("error") if isinstance(event.get("error"), dict) else event
            message = err.get("message") if isinstance(err, dict) else None
            if isinstance(message, str):
                out["errors"].append(message)
    return out


async def _run_codex(
    *, binary, prompt, system, model, effort, images, resume, workdir, timeout_s, env, persist, started
) -> Turn:
    argv = codex_argv(
        binary=binary,
        model=model,
        effort=effort,
        system=system,
        images=images,
        resume=resume,
        persist=persist,
    )
    run_env = claude.build_env(env, binary)
    try:
        raw = await claude.execute(
            argv, cwd=str(workdir), env=run_env, stdin_text=prompt, timeout_s=timeout_s
        )
    except OSError as exc:
        return _fail(
            "codex",
            "start",
            f"codex не запустился ({exc.__class__.__name__})",
            session=resume,
            started=started,
        )
    if raw.timed_out:
        return _fail(
            "codex",
            "timeout",
            f"codex: ход дольше {timeout_s:g} с",
            session=resume,
            started=started,
            model=model,
        )
    parsed = parse_codex(raw.stdout)
    usage = parsed["usage"] or {}
    common = {
        "engine": "codex",
        "model": model,
        "tokens_in": _int(usage.get("input_tokens")),
        "tokens_out": _int(usage.get("output_tokens")),
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    if raw.returncode == 0 and not parsed["failed"] and parsed["text"] is not None:
        return Turn(ok=True, text=parsed["text"], session=parsed["thread"] or resume, **common)
    if await _logged_in("codex", binary, run_env) is False:
        return Turn(ok=False, session=resume, error=_auth_text("codex"), failure="auth", auth=True, **common)
    detail = "; ".join(parsed["errors"]) or raw.stderr.strip()[-200:]
    failure = f"exit {raw.returncode}" if raw.returncode else "garbage"
    return Turn(
        ok=False,
        session=resume,
        error=f"codex: {failure} — {detail}"[:ERROR_TEXT_MAX],
        failure=failure,
        **common,
    )


# --- cursor -------------------------------------------------------------------------------------------------


def cursor_outside_deny(workdir: Path) -> list[str]:
    """Запреты чтения ВНЕ рабочего каталога (проба 22.10: файлы каталога вне `allow` Cursor и так не читает,
    а `/etc/hostname` — читает; `deny` главнее `allow`, поэтому `Read(/**)` закрыл бы и картинки). Каждый
    уровень пути от корня до каталога: все соседи — `Read(/<путь>/**)` (обычные файлы — `Read(<путь>)`).
    Список пересобирается перед каждым ходом."""
    out: list[str] = []
    target = Path(os.path.realpath(workdir))
    parts = target.parts  # ("/", "run", "user", "1000", "hub-brain", "cursor-…")
    for depth in range(1, len(parts)):
        parent = Path(*parts[:depth])
        keep = parts[depth]
        try:
            entries = sorted(os.listdir(parent))
        except OSError:
            continue
        for name in entries:
            if name == keep:
                continue
            path = parent / name
            out.append(f"Read({path}/**)" if path.is_dir() else f"Read({path})")  # ссылка на каталог — тоже
    return out


def cursor_permissions(workdir: Path, allowed: Iterable[Path]) -> dict:
    """`.cursor/cli.json` (раздел 2, проба 22.10): чтение — только перечисленным файлам (пути относительно
    каталога) — внутри каталога прочее Cursor не читает сам, снаружи закрыто `cursor_outside_deny`; оболочка,
    запись, MCP и сеть — запрет."""
    allow = [f"Read({os.path.relpath(path, workdir)})" for path in allowed]
    return {"permissions": {"allow": allow, "deny": [*CURSOR_DENY, *cursor_outside_deny(workdir)]}}


def _cursor_stage(workdir: Path, images: Sequence[Path]) -> list[Path]:
    """Копии картинок хода в `<каталог>/img/` и `cli.json`, разрешающий читать ВСЕ картинки каталога (прежние
    ходы на них ссылаются). -> копии этого хода."""
    img_dir = workdir / CURSOR_IMG_DIR
    img_dir.mkdir(mode=0o700, exist_ok=True)
    copies = []
    for path in images:
        n = len(list(img_dir.iterdir())) + 1
        target = img_dir / f"{n}{path.suffix.lower() or '.png'}"
        shutil.copyfile(path, target)
        copies.append(target)
    conf_dir = workdir / ".cursor"
    conf_dir.mkdir(mode=0o700, exist_ok=True)
    allowed = sorted(img_dir.iterdir())
    (conf_dir / "cli.json").write_text(json.dumps(cursor_permissions(workdir, allowed)), encoding="utf-8")
    return copies


def cursor_prompt(prompt: str, system: str, copies: Sequence[Path], workdir: Path, *, first: bool) -> str:
    parts = []
    if first and system:
        parts.append(f"{CURSOR_SYSTEM_OPEN}\n{CURSOR_SYSTEM_HEAD}\n\n{system}\n{CURSOR_SYSTEM_CLOSE}")
    parts.append(prompt)
    if copies:
        names = ", ".join(os.path.relpath(p, workdir) for p in copies)
        parts.append(f"Картинки к этой реплике — файлы в рабочем каталоге (прочитай их): {names}")
    return "\n\n".join(parts)


async def _run_cursor(
    *, binary, prompt, system, model, effort, images, resume, workdir, timeout_s, env, persist, started
) -> Turn:
    try:
        name, _warn = choice.cursor_model(model, effort)
    except choice.EngineUnavailable as exc:
        return _fail("cursor", "model", f"cursor: {exc}", session=resume, started=started)
    copies = _cursor_stage(Path(workdir), images)
    argv = [
        binary, "-p", "--mode", "ask", "--trust", "--output-format", "json", "--model", name,
        "--workspace", str(workdir),
    ]  # fmt: skip
    if resume:
        argv += ["--resume", resume]
    text = cursor_prompt(prompt, system, copies, Path(workdir), first=resume is None)
    run_env = claude.build_env(env, binary)
    try:
        raw = await claude.execute(argv, cwd=str(workdir), env=run_env, stdin_text=text, timeout_s=timeout_s)
    except OSError as exc:
        return _fail(
            "cursor",
            "start",
            f"cursor не запустился ({exc.__class__.__name__})",
            session=resume,
            started=started,
        )
    if raw.timed_out:
        return _fail(
            "cursor",
            "timeout",
            f"cursor: ход дольше {timeout_s:g} с",
            session=resume,
            started=started,
            model=name,
        )
    data = claude.parse_output(raw.stdout) or {}
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    tokens_in = (
        sum(_int(usage.get(k)) or 0 for k in ("inputTokens", "cacheReadTokens", "cacheWriteTokens")) or None
    )
    common = {
        "engine": "cursor",
        "model": name,
        "tokens_in": tokens_in,
        "tokens_out": _int(usage.get("outputTokens")),
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    result = data.get("result")
    if raw.returncode == 0 and data.get("is_error") is False and isinstance(result, str):
        sid = data.get("session_id") if isinstance(data.get("session_id"), str) else None
        return Turn(ok=True, text=result, session=sid or resume, **common)
    if await _logged_in("cursor", binary, run_env) is False:
        return Turn(ok=False, session=resume, error=_auth_text("cursor"), failure="auth", auth=True, **common)
    detail = (
        (result if isinstance(result, str) else "") or raw.stderr.strip()[-200:] or raw.stdout.strip()[-200:]
    )
    failure = f"exit {raw.returncode}" if raw.returncode else "garbage"
    return Turn(
        ok=False,
        session=resume,
        error=f"cursor: {failure} — {detail}"[:ERROR_TEXT_MAX],
        failure=failure,
        **common,
    )


# --- opencode ----------------------------------------------------------------------------------------------


async def _deny(_req):
    from .oc_turn import Deny

    return Deny("разовый ход: инструменты закрыты")


async def _run_opencode(
    *,
    binary,
    prompt,
    system,
    model,
    effort,
    images,
    resume,
    workdir,
    timeout_s,
    env,
    persist,
    started,
    config_dir,
    home,
) -> Turn:
    import httpx

    from . import oc_config
    from . import opencode_run as opencode
    from .oc_server import EngineStartError, OcServer, SessionNotFound
    from .oc_turn import classify_error, run_turn

    agent = "hub-oneshot"
    permission = {"*": "deny"}
    provider, sep, model_id = model.partition("/")
    if not sep:
        return _fail(
            "opencode",
            "model",
            f"opencode: модель «{model}» — нужно «провайдер/модель»",
            session=resume,
            started=started,
        )
    try:
        overrides = choice.load_overrides(config_dir) if config_dir is not None else {}
    except choice.EngineUnavailable as exc:
        return _fail("opencode", "start", f"OpenCode: {exc}", session=resume, started=started)
    conf_dir = Path(workdir) / ".oc"
    conf_dir.mkdir(mode=0o700, exist_ok=True)
    config_path = conf_dir / "opencode.json"
    oc_config.write(
        config_path,
        oc_config.build(
            "agent", agent_name=agent, permission=permission, mcp_hub=None, overrides=overrides,
            description="hub: разовый ход без инструментов",
        ),
    )  # fmt: skip
    run_env = opencode.build_env(env, binary, overrides=overrides, home=home)
    files = []
    for path in images:
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        mime = _image_mime(path)
        files.append(
            {"type": "file", "mime": mime, "filename": path.name, "url": f"data:{mime};base64,{data}"}
        )
    server = OcServer(binary, log_file=conf_dir / "engine.log")
    common = {"engine": "opencode", "model": model}
    try:
        try:
            await server.start_locked(str(workdir), run_env, config_path)
        except EngineStartError as exc:
            return _fail(
                "opencode", "start", f"OpenCode не запустился: {exc}", session=resume, started=started
            )
        async with server.client(str(workdir)) as client:
            problem = oc_config.check_agent(await client.agents(), agent, permission)
            if problem is not None:
                return _fail("opencode", "start", f"OpenCode: {problem}", session=resume, started=started)
            sid = resume or (await client.create_session("hub: разовый ход", agent))["id"]
            since_ms = int(time.time() * 1000)
            result = await run_turn(
                client, sid, text=prompt, system=system, agent=agent, model=(provider, model_id),
                variant=effort, files=files or None, decide=_deny, timeout_s=timeout_s,
            )  # fmt: skip
            cost = result.cost
            with contextlib.suppress(httpx.HTTPError, SessionNotFound, ValueError):
                _n, total = opencode.turn_stats(await client.messages(sid), since_ms)
                cost = total if total is not None else cost
            tokens = result.tokens if isinstance(result.tokens, dict) else {}
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            common.update(
                cost_usd=cost,
                tokens_in=(_int(tokens.get("input")) or 0) + (_int(cache.get("read")) or 0) or None,
                tokens_out=_int(tokens.get("output")),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            if not persist:
                with contextlib.suppress(httpx.HTTPError, SessionNotFound):
                    await client.delete_session(sid)
            if result.ok:
                return Turn(ok=True, text=result.text, session=sid if persist else None, **common)
            kind = classify_error(result.error, result.retry_s)
            err = result.error or {}
            text = f"OpenCode: {err.get('name') or 'ошибка'}: {str(err.get('message') or '')[:200]}"
            if kind in ("limit", "retry"):
                # Ф83-1: долгий `retry` OpenCode — та же пауза, но `failure="retry"` (не повод для запасного).
                return Turn(ok=False, session=resume, error=text, failure=kind, pause=True, **common)
            if kind == "auth":
                return Turn(
                    ok=False,
                    session=resume,
                    error=_auth_text("opencode"),
                    failure="auth",
                    auth=True,
                    **common,
                )
            failure = "timeout" if err.get("name") == "timeout" else ("model" if kind == "model" else "fail")
            return Turn(ok=False, session=resume, error=text[:ERROR_TEXT_MAX], failure=failure, **common)
    except SessionNotFound:
        return _fail(
            "opencode", "no_session", f"OpenCode: разговора {resume} нет", session=None, started=started
        )
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        return _fail(
            "opencode",
            "fail",
            f"OpenCode: сбой API ({exc.__class__.__name__})",
            session=resume,
            started=started,
        )
    finally:
        await asyncio.shield(server.stop())


# --- вход ---------------------------------------------------------------------------------------------------


async def run(
    engine: str,
    *,
    prompt: str,
    system: str,
    model: str,
    effort: str | None,
    images: Sequence[Path | str] = (),
    resume: str | None = None,
    workdir: Path | str,
    timeout_s: float,
    env: dict | None = None,
    persist: bool = True,
    config_dir: Path | None = None,
    home: Path | None = None,
    binary: str | None = None,
) -> Turn:
    """Один ход модели без инструментов (3.5). `workdir` — существующий пустой каталог вызывающего
    (`Workspace`, П2); `images` — пути к png/jpeg/gif/webp; `resume` — `Turn.session` прошлого УДАЧНОГО хода;
    `persist=False` — разговор не сохраняется (claude `--no-session-persistence`, codex `--ephemeral`,
    opencode — сессия удаляется; cursor так не умеет — чистит `Workspace.close`); `env` — окружение (по
    умолчанию `os.environ`, в процесс идёт разрешающим списком); `config_dir` — каталог конфига хаба (пути
    CLI и переопределения OpenCode из `engine.json`); `binary` — путь к CLI мимо поиска (тесты, `claude_path`
    службы фактов). Отмена (`CancelledError`) гасит группу процессов и пробрасывается дальше."""
    started = time.monotonic()
    home = Path.home() if home is None else Path(home)
    environ = dict(os.environ if env is None else env)
    if engine not in ENGINES:
        return _fail(engine, "engine", f"неизвестный движок {engine}", session=resume, started=started)
    try:
        pictures = _images(images)
    except ValueError as exc:
        return _fail(engine, "image", str(exc), session=resume, started=started)
    try:
        path = _binary(engine, config_dir, home, binary)
    except choice.EngineUnavailable as exc:
        return _fail(engine, "start", f"{engine}: {exc}", session=resume, started=started)
    common = {
        "binary": path,
        "prompt": prompt,
        "system": system,
        "model": model,
        "effort": effort,
        "images": pictures,
        "resume": resume,
        "workdir": Path(workdir),
        "timeout_s": float(timeout_s),
        "env": environ,
        "persist": persist,
        "started": started,
    }
    if engine == "claude":
        return await _run_claude(**common)
    if engine == "codex":
        return await _run_codex(**common)
    if engine == "cursor":
        return await _run_cursor(**common)
    return await _run_opencode(**common, config_dir=config_dir, home=home)
