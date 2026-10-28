"""Причина отказа движка (Ф83-1, docs/architecture.md, П4, П5): одна классификация на
четыре движка — `limit | balance | auth | model | retry | other` (Ф85-1: и `refusal | transient` — их
сообщает только агент владельца).

- `limit` — лимит подписки или провайдера (Claude 5 ч / неделя, Codex, Cursor, 429 провайдера OpenCode);
- `balance` — кончился баланс или ключ без денег (DeepSeek «Insufficient Balance», 402, `insufficient_quota`,
  Cursor `USAGE_PRICING_REQUIRED`);
- `auth` — нет входа или ключа;
- `model` — модели нет у провайдера;
- `retry` — OpenCode долго (дольше `RETRY_LIMIT_S`) повторяет запрос сам: провайдер лежит или сеть (П5: не
  повод переходить на запасной; пауза — как была);
- `other` — всё прочее (сеть, падение, плохой ответ, лимит только Opus у Claude — Claude Code сам уходит на
  Sonnet, П5);
- `refusal` — модель отказалась выполнять поручение (Ф85-1, C.1; классификатор её не ставит — сообщает агент);
- `transient` — восстанавливаемый сбой: обрыв, 5xx, таймаут (Ф85-1; сообщает агент).

Повод перейти на запасной: у сессий хаба — `SESSION_REASONS` (П4), у фактов — `BACKGROUND_REASONS`, у агента
владельца — `AGENT_REASONS` (Ф85-1: те же плюс `refusal` и `transient`; `auth` — только после двух проверок входа,
это забота вызывающего). Тексты Codex и
Cursor живьём не получены: маркеры — известные, незнакомое — `other`.

Модуль без зависимостей (только `re`): его берут ядро, хост, драйверы движков и служба фактов."""

import re

KINDS = ("limit", "balance", "auth", "model", "retry", "other", "refusal", "transient")
SESSION_REASONS = frozenset({"limit", "balance"})
BACKGROUND_REASONS = frozenset({"limit", "balance", "model", "auth"})
# Ф85-1 (C.2): у агента владельца отказ модели и восстанавливаемый сбой — тоже повод перейти на запасной.
AGENT_REASONS = BACKGROUND_REASONS | {"refusal", "transient"}
# Как `oc_turn.RETRY_LIMIT_S` (там же и значение для хода): дольше — провайдер лежит.
RETRY_LIMIT_S = 600.0

LABELS = {
    "limit": "лимит",
    "balance": "кончился баланс",
    "auth": "нет входа",
    "model": "модели нет",
    "retry": "провайдер не отвечает",
    "other": "сбой",
    "refusal": "модель отказалась",
    "transient": "временный сбой",
}

# --- маркеры ------------------------------------------------------------------------------------------------

_AUTH_WORDS = (
    "api key",
    "api_key",
    "apikey",
    "unauthorized",
    "authentication",
    "/login",
    "please run /login",
    "not logged in",
    "logged out",
    "oauth token",
    "invalid api key",
    "codex login",
    "cursor-agent login",
)
_BALANCE_WORDS = (
    "insufficient",
    "balance",
    "credit",
    "billing",
    "payment required",
    "quota",
    "usage pricing required",
    "usage_pricing_required",
    "spend limit",
    "spending limit",
    "out of credits",
)
_LIMIT_WORDS = (
    "rate limit",
    "rate_limit",
    "ratelimit",
    "too many requests",
    "usage limit",
    "usage_limit",
    "limit reached",
    "reached your limit",
    "rate_limit_error",
    "usagelimitexceeded",
    "ratelimitexceeded",
    "hit your limit",
    "hit the limit",
    "out of requests",
    "out of usage",
    "out of fast requests",
)
_MODEL_RE = re.compile(
    r"model[^\n]{0,80}?(not found|not_found|does not exist|is not supported|unknown)"
    r"|(unknown|invalid|unsupported) model|model_not_found|not_found_error",
    re.IGNORECASE,
)
_OPUS_RE = re.compile(r"opus", re.IGNORECASE)
# Код HTTP — отдельным числом: «214020 tokens» — не 402, «214290» — не 429 (ревью Ф83).
_CODE_402 = re.compile(r"(?<!\d)402(?!\d)")
_CODE_429 = re.compile(r"(?<!\d)429(?!\d)")
# Временная перегрузка провайдера — не «модели нет» (П5: провайдер и сеть — не повод).
_TRANSIENT_WORDS = (
    "temporarily unavailable",
    "overloaded",
    "please retry",
    "try again later",
    '"unavailable"',
)


def _has(text: str, words) -> bool:
    return any(w in text for w in words)


def _text_kind(text: str, *, quota_is_limit: bool = False) -> str:
    """Причина по тексту отказа (уже в нижнем регистре). `quota_is_limit` — у Cursor «quota exceeded» — лимит
    подписки, а не баланс."""
    if _has(text, _AUTH_WORDS):
        return "auth"
    balance = [w for w in _BALANCE_WORDS if not (quota_is_limit and w == "quota")]
    if _CODE_402.search(text) or _has(text, balance):
        return "balance"
    if _CODE_429.search(text) or _has(text, _LIMIT_WORDS) or (quota_is_limit and "quota" in text):
        return "limit"
    if _has(text, _TRANSIENT_WORDS):
        return "other"
    if _MODEL_RE.search(text):
        return "model"
    return "other"


def _oc_error(error: dict | None, retry_s: float | None) -> str:
    """OpenCode: ошибка хода `{name, message, status?}` и сколько секунд сессия была в `retry`."""
    if retry_s is not None and retry_s >= RETRY_LIMIT_S:
        return "retry"
    if not isinstance(error, dict):
        return "other"
    name = str(error.get("name") or "")
    message = str(error.get("message") or "").lower()
    status = error.get("status")
    if name == "ProviderAuthError" or status == 401 or _has(message, _AUTH_WORDS[:5]):
        return "auth"
    if status == 402 or _has(message, _BALANCE_WORDS):
        return "balance"
    if status == 429 or _CODE_429.search(message) or _has(message, _LIMIT_WORDS):
        return "limit"
    if (isinstance(status, int) and status >= 500) or _has(message, _TRANSIENT_WORDS):
        return "other"
    if "model not found" in message or _MODEL_RE.search(message):
        return "model"
    return "other"


def _rate_limit_info(engine: str, info: dict) -> str:
    """Событие `rate_limit_event` хоста. Явный `kind` (его кладут драйверы OpenCode, Codex, Cursor) — как
    есть. Claude Code: отказ только окна Opus — `other` (П5: Claude Code сам уходит на Sonnet)."""
    named = info.get("kind")
    if named in KINDS:
        return named
    if engine == "claude":
        if info.get("status") != "rejected":
            return "other"
        window = " ".join(str(info.get(k) or "") for k in ("rateLimitType", "limitType", "window"))
        return "other" if _OPUS_RE.search(window) else "limit"
    reason = str(info.get("reason") or "").lower()
    if engine == "opencode" and ("ключ" in reason or "auth login" in reason):
        return "auth"
    kind = _text_kind(reason, quota_is_limit=engine == "cursor") if reason else "other"
    if kind == "other" and info.get("status") == "rejected":
        return "limit"  # отказ без понятного текста — лимит подписки (так его и ставит хост)
    return kind


def _turn(engine: str, turn) -> str:
    """Итог разового хода `oneshot.Turn` (агент владельца, факты)."""
    if getattr(turn, "ok", False):
        return "other"
    extra = getattr(turn, "extra", None)
    if isinstance(extra, dict) and extra.get("kind") in KINDS:
        return extra["kind"]
    failure = str(getattr(turn, "failure", None) or "")
    text = str(getattr(turn, "error", None) or "").lower()
    if getattr(turn, "auth", False) or failure == "auth":
        return "auth"
    if failure == "retry":
        return "retry"
    if failure == "model":
        return "model"
    if getattr(turn, "pause", False) or failure == "limit":
        kind = _text_kind(text, quota_is_limit=engine == "cursor")
        return "balance" if kind == "balance" else "limit"
    return _text_kind(text, quota_is_limit=engine == "cursor")


def classify(engine: str, source, *, retry_s: float | None = None) -> str:
    """Причина отказа движка `engine` -> один из `KINDS`.

    `source`: событие `rate_limit_event` (`rate_limit_info`, словарь со `status`/`resetsAt`/`reason`/`kind`),
    ошибка хода OpenCode (`{name, message, status?}`; `retry_s` — секунды в `retry`), итог разового хода
    (`oneshot.Turn`) или текст отказа (результат, stderr). Непонятное — `other`."""
    name = engine if isinstance(engine, str) else ""
    if source is None:
        return "retry" if name == "opencode" and retry_s is not None and retry_s >= RETRY_LIMIT_S else "other"
    if isinstance(source, dict):
        if "status" in source and ("resetsAt" in source or "reason" in source or "kind" in source):
            return _rate_limit_info(name, source)
        if source.get("status") == "rejected" or source.get("kind") in KINDS:
            return _rate_limit_info(name, source)
        if name == "opencode" or "name" in source:
            return _oc_error(source, retry_s)
        return _text_kind(str(source.get("message") or "").lower(), quota_is_limit=name == "cursor")
    if isinstance(source, str):
        return _text_kind(source.lower(), quota_is_limit=name == "cursor")
    if hasattr(source, "failure") and hasattr(source, "ok"):
        return _turn(name, source)
    return "other"


def label(kind: str | None) -> str:
    return LABELS.get(kind or "", LABELS["other"])


# --- ключ записи паузы (движок или движок:провайдер) --------------------------------------------------------

# Движки, у которых модель — «провайдер/модель» и лимит или ключ одного провайдера не держит другие: пауза
# пишется на `движок:провайдер`. Остальные — на имя движка, как было.
PROVIDER_ENGINES = frozenset({"opencode"})


def model_provider(engine: str | None, model) -> str | None:
    """Провайдер модели `провайдер/модель` у движка с провайдерами; иначе (или модель не такая) — `None`."""
    if engine not in PROVIDER_ENGINES or not isinstance(model, str):
        return None
    head, sep, tail = model.strip().partition("/")
    return head if sep and head and tail else None


def pause_key(engine: str, provider: str | None = None) -> str:
    """Ключ записи паузы: `движок:провайдер` у движка с провайдерами (провайдер известен), иначе — имя
    движка."""
    if provider and engine in PROVIDER_ENGINES:
        return f"{engine}:{provider}"
    return engine


def key_engine(key) -> str | None:
    """Движок ключа записи паузы (`opencode:anthropic` -> `opencode`); не строка — `None`."""
    if not isinstance(key, str) or not key:
        return None
    return key.partition(":")[0]


def pause_keys(engine: str, provider: str | None = None) -> tuple[str, ...]:
    """Ключи, которые держат сессию движка `engine` с провайдером `provider`: свой `движок:провайдер` и
    запись без провайдера (её пишут движки без провайдеров и старый хост — обратная совместимость)."""
    own = pause_key(engine, provider)
    return (own,) if own == engine else (own, engine)
