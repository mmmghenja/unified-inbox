"""Разовый запуск `claude -p` (вынесено из дежурной `hub-wake`, Ф57-2): окружение, процесс, итог, вход.

- Окружение — разрешающим списком (П9): никакие `ANTHROPIC_*`/`CLAUDE_*` не передаются — иначе запуск ушёл бы
  в платный API или на чужой адрес вместо подписки.
- Процесс — в своей группе (`start_new_session`): по таймауту и по остановке гасится вся группа
  (SIGTERM, через 10 с SIGKILL), вместе с MCP-сервером хаба.
- Разбор результата — `classify`: успех / отказ (вид) / пауза подписки (П8) / «сессия уже есть» /
  «сессии нет». Потребители: служба фактов, хост рабочих сессий (проверка входа).
"""

import asyncio
import contextlib
import json
import os
import re
import signal
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta

KILL_GRACE_S = 10.0
AUTH_TIMEOUT_S = 20.0
STDERR_KEEP = 300

# П9: окружение `claude` — только эти переменные (и `LC_*`).
ENV_ALLOW = (
    "HOME",
    "USER",
    "LOGNAME",
    "PATH",
    "LANG",
    "TZ",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
    "TERM",
)

# W5 + мелочи: «сессия уже есть» / «сессии нет» — по коду выхода и тексту с запасом (текст — не контракт).
IN_USE_MARKERS = ("already in use",)
NO_SESSION_MARKERS = (
    "no conversation found",
    "conversation not found",
    "session not found",
    "no session found",
    "no such session",
    "could not find session",
    "could not find conversation",
)
# П8: исчерпан лимит подписки или проблема входа — пауза всего демона, а не отказ карточек. Ревью F2: в тексте
# `result` (там бывает пересказ чужой переписки) — только узкие маркеры; широкие слова — только в stderr CLI.
LIMIT_MARKERS_RESULT = (
    "usage limit",
    "limit reached",
    "reached your limit",
    "rate_limit_error",
    "/login",
    "oauth token",
    "invalid api key",
    "please run /login",
)
LIMIT_MARKERS_STDERR = (
    *LIMIT_MARKERS_RESULT,
    "login",
    "log in",
    "logged in",
    "authentication",
    "credit",
    "api key",
    "unauthorized",
    "rate limit",
)
_RESET_EPOCH_RE = re.compile(r"\|\d{10}\b")


def build_env(environ: dict, claude: str) -> dict:
    """П9: копия окружения демона разрешающим списком; `PATH` дополняется каталогом `claude`."""
    env = {k: v for k, v in environ.items() if k in ENV_ALLOW or k.startswith("LC_")}
    claude_dir = os.path.dirname(claude)
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    if claude_dir and claude_dir not in parts:
        parts.append(claude_dir)
    env["PATH"] = os.pathsep.join(parts) if parts else claude_dir
    return env


# --- процесс ------------------------------------------------------------------------------------------------


@dataclass
class Outcome:
    kind: str  # ok | fail | pause | in_use | no_session
    failure: str | None = None  # вид отказа: timeout, exit N, is_error <subtype>, garbage, error_max_turns
    data: dict | None = None
    returncode: int | None = None
    stderr: str = ""
    reset_ts: float | None = None  # П8: время сброса лимита из текста, если нашлось
    retryable: bool = True
    extra: dict = field(default_factory=dict)

    @property
    def result_text(self) -> str:
        value = (self.data or {}).get("result")
        return value if isinstance(value, str) else ""

    @property
    def denials(self) -> int:
        value = (self.data or {}).get("permission_denials")
        return len(value) if isinstance(value, list) else 0


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM группе, через `KILL_GRACE_S` — SIGKILL; остатки группы (MCP-сервер) — SIGKILL в конце."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), KILL_GRACE_S)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


@dataclass
class Raw:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool


async def execute(argv: list[str], *, cwd: str, env: dict, stdin_text: str, timeout_s: float) -> Raw:
    """Один процесс `claude`. Отмена задачи (остановка демона, П11) гасит группу и пробрасывается дальше."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin_text.encode("utf-8")), timeout_s)
    except TimeoutError:
        await _kill_group(proc)
        return Raw(proc.returncode, "", "", True)
    except BaseException:
        await asyncio.shield(_kill_group(proc))
        raise
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)  # остатки группы после штатного выхода
    return Raw(proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), False)


def parse_output(stdout: str) -> dict | None:
    """`--output-format json` — один объект; на всякий случай — последняя строка-объект."""
    text = stdout.strip()
    if not text:
        return None
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


_EPOCH_RE = re.compile(r"\|(\d{10})\b")
_RESET_AT_RE = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)?", re.IGNORECASE)
_IN_RE = re.compile(r"(?:try again|retry|resets?)\s+in\s+(\d+)\s*(minute|min|hour|h)", re.IGNORECASE)


def parse_reset(text: str, now_s: float) -> float | None:
    """П8: время сброса лимита из текста (эпоха после `|`, «resets 5pm», «try again in 2 hours»). Только
    будущее и не дальше суток — иначе `None` (демон берёт свою паузу)."""
    candidate: float | None = None
    m = _EPOCH_RE.search(text)
    if m:
        candidate = float(m.group(1))
    if candidate is None:
        m = _IN_RE.search(text)
        if m:
            n = int(m.group(1))
            candidate = now_s + n * (3600 if m.group(2).lower().startswith("h") else 60)
    if candidate is None:
        m = _RESET_AT_RE.search(text)
        if m:
            hour, minute = int(m.group(1)), int(m.group(2) or 0)
            ampm = (m.group(3) or "").lower()
            if ampm == "pm" and hour < 12:
                hour += 12
            if ampm == "am" and hour == 12:
                hour = 0
            if hour < 24 and minute < 60:
                now = datetime.fromtimestamp(now_s)
                moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if moment <= now:
                    moment += timedelta(days=1)
                candidate = moment.timestamp()
    if candidate is None or candidate <= now_s or candidate > now_s + 86400:
        return None
    return candidate


def classify(raw: Raw, now_s: float) -> Outcome:
    stderr = raw.stderr[:STDERR_KEEP]
    if raw.timed_out:
        return Outcome("fail", "timeout", stderr=stderr)
    data = parse_output(raw.stdout)
    rc = raw.returncode
    if data is not None and rc == 0 and data.get("is_error") is False:
        return Outcome("ok", data=data, returncode=rc, stderr=stderr)
    low = (raw.stderr + "\n" + (raw.stdout if data is None else "")).lower()
    if rc != 0 and any(m in low for m in IN_USE_MARKERS):
        return Outcome("in_use", "session in use", data=data, returncode=rc, stderr=stderr)
    if rc != 0 and any(m in low for m in NO_SESSION_MARKERS):
        return Outcome("no_session", "no session", data=data, returncode=rc, stderr=stderr)
    subtype = data.get("subtype") if data is not None else None
    if subtype == "error_max_turns":
        # П7: исчерпан лимит ходов — повтор не поможет (и удвоит черновики).
        return Outcome("fail", "error_max_turns", data=data, returncode=rc, stderr=stderr, retryable=False)
    result = data.get("result") if data is not None else None
    narrow = (
        (result if isinstance(result, str) else "") + "\n" + (raw.stdout if data is None else "")
    ).lower()
    err = raw.stderr.lower()
    blob = narrow + "\n" + err
    if (
        any(m in narrow for m in LIMIT_MARKERS_RESULT)
        or _RESET_EPOCH_RE.search(narrow)
        or any(m in err for m in LIMIT_MARKERS_STDERR)
        or _RESET_EPOCH_RE.search(err)
    ):
        return Outcome(
            "pause",
            "лимит подписки или вход",
            data=data,
            returncode=rc,
            stderr=stderr,
            reset_ts=parse_reset(blob, now_s),
        )
    if data is None:
        return Outcome("fail", "garbage" if rc == 0 else f"exit {rc}", returncode=rc, stderr=stderr)
    if data.get("is_error") or rc != 0:
        kind = f"is_error {subtype}" if data.get("is_error") else f"exit {rc}"
        return Outcome("fail", kind, data=data, returncode=rc, stderr=stderr)
    return Outcome("fail", "garbage", data=data, returncode=rc, stderr=stderr)


# --- claude auth status (П9) --------------------------------------------------------------------------------


def auth_status(claude: str, env: dict) -> tuple[bool, str]:
    """`claude auth status --json` (команда локальная) в том же урезанном окружении, что и запуск. Подписка —
    `loggedIn`, `authMethod == "claude.ai"`, `apiProvider == "firstParty"`. Почта и организация не выводятся.
    Возвращает (подписка ли, текст для status)."""
    if not os.path.exists(claude):
        return False, f"claude не найден: {claude}"
    try:
        proc = subprocess.run(  # noqa: S603 — путь из конфига демона, без shell
            [claude, "auth", "status", "--json"],
            env=env,
            capture_output=True,
            text=True,
            timeout=AUTH_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"claude auth status не выполнился ({exc.__class__.__name__})"
    data = parse_output(proc.stdout)
    if data is None:
        return False, f"claude auth status: неразборчивый вывод (код {proc.returncode})"
    method = data.get("authMethod")
    provider = data.get("apiProvider")
    sub = data.get("subscriptionType")
    summary = f"вход {method or '?'}, провайдер {provider or '?'}, подписка {sub or '—'}"
    if data.get("loggedIn") is not True:
        return False, "claude не залогинен (claude auth login)"
    if method != "claude.ai" or provider not in (None, "firstParty"):
        return False, f"вход не по подписке — {summary}"
    return True, summary
