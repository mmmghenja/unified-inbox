"""Токены, вход, CSRF, троттлинг, Principal. Спецификация: docs/architecture.md,
«Bearer», «Principal и скоуп».

Время всегда приходит параметром `now_ms` — здесь `time.time()` не звать (правило раздела «Время» спеки).
Функции, трогающие БД (`login`, `create_api_token`, `revoke_token`), вызываются только внутри `Db.write()`,
как и в `outbox.py`/`ingest.py`.
"""

import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import NamedTuple

from fastapi import HTTPException, Request

_DAY_MS = 24 * 60 * 60 * 1000
LOGIN_CODE_TTL_MS = 60_000
UI_SESSION_TTL_MS = 30 * _DAY_MS
# Ф10-7, решение 6: продление UI-сессии при использовании — чаще раза в 10 минут без записи.
RENEW_MIN_INTERVAL_MS = 10 * 60 * 1000
USER_AGENT_MAX = 300
VIA_LOCAL = "local"
VIA_REMOTE = "remote"
BEARER_PREFIX = "hub_"
_CSRF_SALT = "hub-csrf:"

# Троттлинг неудач (bearer и логин-код): 10 неудач за 60 с -> 429 на 60 с для ВСЕХ попыток этого вида.
THROTTLE_FAILURES = 10
THROTTLE_WINDOW_MS = 60_000
THROTTLE_BLOCK_MS = 60_000


class AuthError(Exception):
    """Базовая ошибка входа/аутентификации; текст — только для логов, не для ответа клиенту."""


class AmbiguousPrincipal(AuthError):
    """И Bearer, и cookie в одном запросе — неоднозначный принципал (400)."""


@dataclass(frozen=True)
class Principal:
    token_id: int
    kind: str  # 'ui' | 'interactive' | 'background'
    scopes: frozenset[str]
    project_ids: frozenset[int] | None  # None = все проекты (только UI)

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    @property
    def is_ui(self) -> bool:
        """UI-сессия владельца. НЕ то же, что `project_ids is None`: bearer «на все проекты» тоже без списка, но
        псевдо-проект «Все», `last_error` и UI-only действия ему не положены."""
        return self.kind == "ui"

    @property
    def sees_unsorted(self) -> bool:
        """Спека 7a, A5/П8: псевдо-проект «Неразобранное» (`-1`) — UI-сессия ИЛИ bearer со скоупом `organize`
        на все проекты (раскладке нужно видеть, что раскладывать). Только для веток `UNSORTED_PROJECT_ID`;
        «Все» (`-2`) и `last_error` по-прежнему только `is_ui`."""
        return self.is_ui or (self.has_scope("organize") and self.project_ids is None)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def compute_csrf(cookie_value: str) -> str:
    """csrf = sha256("hub-csrf:" + значение cookie), hex. Без серверного секрета и хранения — см. спеку."""
    return _sha256(_CSRF_SALT + cookie_value)


def verify_csrf(cookie_value: str, provided: str) -> bool:
    return hmac.compare_digest(compute_csrf(cookie_value), provided)


def extract_cookie(cookie_header: str | None, name: str = "hub_session") -> str | None:
    """Достать значение cookie `name` из заголовка Cookie. Общая функция для middleware.py и security.py."""
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


class Throttle:
    """В памяти, per-process. Один экземпляр на приложение — не модульный синглтон (тесты поднимают своё
    приложение на каждый тест и не должны видеть блокировки друг друга).
    """

    def __init__(self) -> None:
        self._failures: dict[str, list[int]] = {}
        self._blocked_until: dict[str, int] = {}

    def blocked(self, kind: str, now_ms: int) -> bool:
        until = self._blocked_until.get(kind)
        return until is not None and now_ms < until

    def record_failure(self, kind: str, now_ms: int) -> None:
        window_start = now_ms - THROTTLE_WINDOW_MS
        fails = [t for t in self._failures.get(kind, []) if t > window_start]
        fails.append(now_ms)
        self._failures[kind] = fails
        if len(fails) >= THROTTLE_FAILURES:
            self._blocked_until[kind] = now_ms + THROTTLE_BLOCK_MS


class LoginResult(NamedTuple):
    ok: bool
    reason: str | None  # 'invalid' | 'used' | 'expired' — только для логов/audit, не для тела ответа
    token: str | None
    token_id: int | None
    csrf: str | None
    expires_ts: int | None


def _audit(conn, *, action: str, result: str, now_ms: int, api_token_id: int | None = None) -> None:
    conn.execute(
        "INSERT INTO main.audit (ts, actor, api_token_id, action, result) VALUES (?, 'auth', ?, ?, ?)",
        (now_ms, api_token_id, action, result),
    )


def create_login_code(conn, now_ms: int) -> str:
    """Одноразовый код входа: 256 бит, в БД — только sha256, TTL 60 с. Вызывается внутри Db.write()."""
    code = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO main.login_code (hash, expires_ts) VALUES (?, ?)",
        (_sha256(code), now_ms + LOGIN_CODE_TTL_MS),
    )
    return code


def login_code_valid(conn, *, code: str, now_ms: int) -> bool:
    """Только чтение: свежий ли и неиспользованный ли код. Нужна, чтобы под троттлингом верный код проходил, а
    неверные отбивались без записи в БД."""
    row = conn.execute(
        "SELECT expires_ts, used_ts FROM main.login_code WHERE hash = ?", (_sha256(code),)
    ).fetchone()
    return row is not None and row[1] is None and row[0] > now_ms


def clip_user_agent(user_agent: str | None) -> str | None:
    """Ф10-7, решение 6: User-Agent входа — не длиннее 300 символов; пустой -> NULL."""
    if not user_agent:
        return None
    return user_agent[:USER_AGENT_MAX]


def create_ui_session(
    conn, *, via: str, user_agent: str | None, now_ms: int, prev_cookie: str | None
) -> tuple[str, int, str, int]:
    """Общий путь создания UI-сессии (вход кодом и вход паролем, Ф10-7, решение 6): строка `api_token`
    с `created_ts`/`last_used_ts`/`via`/`user_agent`; вход поверх живой куки отзывает прежнюю сессию этой
    куки в той же транзакции (ярлык «Hub» зовёт `hub ui` при каждом запуске — иначе копились бы десятки).
    Возвращает `(token, token_id, csrf, expires_ts)`. Вызывается внутри `Db.write()`."""
    if via not in (VIA_LOCAL, VIA_REMOTE):
        raise ValueError(f"create_ui_session: via должен быть local|remote, получено {via!r}")
    if prev_cookie:
        conn.execute(
            "UPDATE main.api_token SET revoked = 1 WHERE hash = ? AND kind = 'ui' AND revoked = 0",
            (_sha256(prev_cookie),),
        )
    token = secrets.token_urlsafe(32)
    expires_ts = now_ms + UI_SESSION_TTL_MS
    cur = conn.execute(
        "INSERT INTO main.api_token (name, kind, hash, scopes, project_scope_json, expires_ts, revoked, "
        "created_ts, last_used_ts, via, user_agent) "
        "VALUES ('ui', 'ui', ?, 'read send admin', NULL, ?, 0, ?, ?, ?, ?)",
        (_sha256(token), expires_ts, now_ms, now_ms, via, clip_user_agent(user_agent)),
    )
    return token, cur.lastrowid, compute_csrf(token), expires_ts


def login(
    conn,
    *,
    code: str,
    now_ms: int,
    user_agent: str | None = None,
    prev_cookie: str | None = None,
) -> LoginResult:
    """Проверить одноразовый код и, если он свежий и не использован, завести UI-сессию (`via='local'`: код
    выдаёт только `hub ui` на ноутбуке, вход кодом на удалённом слушателе запрещён роутом).

    Неудача НЕ бросает исключение — она пишется в audit и коммитится вместе с остальной транзакцией
    (в отличие от исключения, которое откатило бы всё, включая саму запись о попытке).
    """
    code_hash = _sha256(code)
    row = conn.execute(
        "SELECT expires_ts, used_ts FROM main.login_code WHERE hash = ?", (code_hash,)
    ).fetchone()
    if row is None:
        _audit(conn, action="login", result="invalid", now_ms=now_ms)
        return LoginResult(False, "invalid", None, None, None, None)
    expires_ts, used_ts = row
    if used_ts is not None:
        _audit(conn, action="login", result="used", now_ms=now_ms)
        return LoginResult(False, "used", None, None, None, None)
    if expires_ts <= now_ms:
        _audit(conn, action="login", result="expired", now_ms=now_ms)
        return LoginResult(False, "expired", None, None, None, None)

    conn.execute("UPDATE main.login_code SET used_ts = ? WHERE hash = ?", (now_ms, code_hash))

    token, token_id, csrf, expires_ts_new = create_ui_session(
        conn, via=VIA_LOCAL, user_agent=user_agent, now_ms=now_ms, prev_cookie=prev_cookie
    )
    _audit(conn, action="login", result="ok", now_ms=now_ms, api_token_id=token_id)
    return LoginResult(True, None, token, token_id, csrf, expires_ts_new)


def logout(conn, *, token_id: int, now_ms: int) -> None:
    conn.execute("UPDATE main.api_token SET revoked = 1 WHERE id = ?", (token_id,))
    _audit(conn, action="logout", result="ok", now_ms=now_ms, api_token_id=token_id)


# --- UI-сессии: список, отзыв, продление (Ф10-7, решение 6) ---------------------------------------------

_LIVE_UI = "kind = 'ui' AND revoked = 0 AND (expires_ts IS NULL OR expires_ts > ?)"


def list_ui_sessions(conn, *, now_ms: int, current_id: int | None) -> list[dict]:
    """Живые UI-сессии (не отозваны, не истекли), по `last_used_ts` убыв., NULL в конце. Без хешей."""
    rows = conn.execute(
        "SELECT id, via, created_ts, last_used_ts, expires_ts, user_agent FROM main.api_token "  # noqa: S608
        f"WHERE {_LIVE_UI} ORDER BY last_used_ts IS NULL, last_used_ts DESC, id DESC",
        (now_ms,),
    ).fetchall()
    return [
        {
            "id": row[0],
            "via": row[1],
            "created_ts": row[2],
            "last_used_ts": row[3],
            "expires_ts": row[4],
            "user_agent": row[5],
            "current": row[0] == current_id,
        }
        for row in rows
    ]


def revoke_ui_session(conn, *, session_id: int, now_ms: int, actor_token_id: int) -> bool:
    """Отозвать одну живую UI-сессию; `False` — такой живой UI-сессии нет (bearer так не отзывается)."""
    cur = conn.execute(
        f"UPDATE main.api_token SET revoked = 1 WHERE id = ? AND {_LIVE_UI}",  # noqa: S608
        (session_id, now_ms),
    )
    if cur.rowcount == 0:
        return False
    _audit(conn, action="session_revoke", result="ok", now_ms=now_ms, api_token_id=actor_token_id)
    return True


def revoke_all_ui_sessions(conn, *, keep_id: int | None, now_ms: int, actor_token_id: int) -> int:
    """«Выйти везде» (`keep_id=None`) или «везде, кроме этого» (`keep_id` — текущая). Возвращает число."""
    if keep_id is None:
        cur = conn.execute(f"UPDATE main.api_token SET revoked = 1 WHERE {_LIVE_UI}", (now_ms,))  # noqa: S608
    else:
        cur = conn.execute(
            f"UPDATE main.api_token SET revoked = 1 WHERE {_LIVE_UI} AND id != ?",  # noqa: S608
            (now_ms, keep_id),
        )
    _audit(conn, action="session_revoke_all", result="ok", now_ms=now_ms, api_token_id=actor_token_id)
    return cur.rowcount


def revoke_ui_sessions_via(conn, *, via: str) -> int:
    """Отозвать все живые UI-сессии одного слушателя (`remote` — смена пароля и выключение доступа)."""
    cur = conn.execute(
        "UPDATE main.api_token SET revoked = 1 WHERE kind = 'ui' AND revoked = 0 AND via = ?", (via,)
    )
    return cur.rowcount


def ui_session_renewal(conn, *, token_id: int, now_ms: int) -> tuple[int | None, bool]:
    """Только чтение: `(expires_ts, пора_ли_писать)`. Продление пишется не чаще раза в 10 минут
    (`RENEW_MIN_INTERVAL_MS`) — чтобы частые вызовы UI не будили писателя и не будили SSE-потоки."""
    row = conn.execute(
        "SELECT expires_ts, last_used_ts FROM main.api_token WHERE id = ? AND kind = 'ui'", (token_id,)
    ).fetchone()
    if row is None:
        return None, False
    expires_ts, last_used_ts = row
    due = last_used_ts is None or now_ms - last_used_ts >= RENEW_MIN_INTERVAL_MS or now_ms < last_used_ts
    return expires_ts, due


def renew_ui_session(conn, *, token_id: int, now_ms: int) -> int | None:
    """Продлить живую UI-сессию: `expires_ts = now + 30 дней`, `last_used_ts = now`. Внутри `Db.write()`.
    Возвращает новый `expires_ts`; `None` — сессия уже не живая (отозвана между чтением и записью)."""
    expires_ts = now_ms + UI_SESSION_TTL_MS
    cur = conn.execute(
        f"UPDATE main.api_token SET expires_ts = ?, last_used_ts = ? WHERE id = ? AND {_LIVE_UI}",  # noqa: S608
        (expires_ts, now_ms, token_id, now_ms),
    )
    return expires_ts if cur.rowcount else None


def session_still_valid(conn, *, token_id: int, remote: bool, now_ms: int) -> bool:
    """П4: перепроверка сессии открытого SSE-потока — не отозвана, не истекла, слушатель тот же."""
    row = conn.execute(
        "SELECT kind, revoked, expires_ts, via FROM main.api_token WHERE id = ?", (token_id,)
    ).fetchone()
    if row is None:
        return False
    kind, revoked, expires_ts, via = row
    if revoked or (expires_ts is not None and expires_ts <= now_ms):
        return False
    if kind == "ui":
        return via == VIA_REMOTE if remote else via in (None, VIA_LOCAL)
    return not remote


# Спека 7a, A2: скоупы bearer-токена. `admin` — только у UI-сессии, bearer его не получает никогда.
# `send` у bearer со спеки 7a значит то же, что `draft` (всё через bearer — черновик, A1); оставлен ради
# существующих токенов в БД (не мигрируются). `triage` (спека 7b, A3) — запись карточек дежурного агента
# (`POST /agent/items`); разрешён и `background`, и `interactive`.
# `work_host` (Ф9-1, 9a, C) — хост рабочих сессий `hub work run`: ТОЛЬКО `background`, только «все проекты»
# и ровно со скоупами `{work_host, read}` (выдаёт `hub work setup`); в любом другом сочетании — отказ.
# `orchestrate` (Ф9-3, 9c; убран в Ф57-2 вместе с чатом оркестратора) — новым токенам не выдаётся (нет в
# `BEARER_SCOPES`); у старых токенов остаётся, читается без падения, запреты (attach, подписка) сохранены.
# `runs` (Ф11-6 С5; убран в Ф57-2 вместе с дежурной) — новым токенам не выдаётся (нет в `BEARER_SCOPES`);
# у старых токенов остаётся, читается без падения, запреты ниже (attach, подписка, реакции) на них сохранены.
# `attach` (Ф14-3 A1 + П8) — право класть файлы в черновики (`POST /uploads`, `/uploads/from-media`, `files` в
# `/messages/send` от bearer). Только `interactive`, не вместе с `orchestrate`, не токену оркестратора
# `mcp:orchestrator` (`check_attach`).
# `transcribe` (Ф14-6, 14-6a, C и П7) — служба расшифровки `hub transcribe run`: берёт задания и пишет «текст
# голосового». ТОЛЬКО `background`, только «все проекты» и ровно со скоупами `{read, transcribe}` (выдаёт
# `hub transcribe setup`): иначе токен проекта или MCP-профиль брал бы задания чужих проектов.
# `facts` (Ф24-1 A3) — служба фактов о людях `hub facts run`: очередь созревших людей, контекст переписки с
# человеком и запись фактов. Как `transcribe`: ТОЛЬКО `background`, только «все проекты» и ровно
# `{read, facts}`.
# `human` (Ф54-1, П3) — ответчик заявок «нужен человек»: ТОЛЬКО отдельный токен ровно со скоупом `{human}`
# (без `read` и `draft`; проекты можно ограничить). Выпускает `hub human-agent token`, ни у одного профиля его
# нет. Такой токен принимают только ручки заявок: `principal_dependency` отвечает ему 403 везде, где ручка
# сама не объявила `human_ok` (в том числе на ручках без требуемого скоупа).
# `human_bot` (Ф54-2, П5) — бот Telegram, второй вход владельца в очередь заявок: ТОЛЬКО `background`, ровно
# `{human_bot}`, только «все проекты». Как `human` — 403 везде, кроме ручек заявок; в отличие от него видит и
# решает заявки всех видов (он рука владельца, выключатели видов на него не действуют).
# `term` (Ф56-5, К4) — ключ агента владельца к терминалу хаба: ТОЛЬКО `background`, ровно `{term}`, только «все
# проекты». Свой флаг `term_ok`, отдельный от `human_ok`: токену с `term` — 403 везде, кроме ручек терминала,
# объявивших `term_ok` (список, создание, запуск команды, экран); ручкам заявок и канала владельца он чужой.
# `listen` (Ф60-1, П7) — служба записи интервью `hub-listen`: по образцу `term` — ТОЛЬКО `background`, ровно
# `{listen}`, только «все проекты», `attach` не дописывается. Свой флаг `listen_ok`: токену с `listen` — 403
# везде, кроме ручек `/listen/*` (они же отвечают 403 куке и любому другому bearer).
BEARER_SCOPES = frozenset(
    {
        "read",
        "draft",
        "send",
        "organize",
        "triage",
        "work_host",
        "attach",
        "transcribe",
        "facts",
        "human",
        "human_bot",
        "term",
        "listen",
        "notify",
        "patterns_ext",
        "digest",
    }
)
WORK_HOST_SCOPES = frozenset({"work_host", "read"})
TRANSCRIBE_SCOPES = frozenset({"read", "transcribe"})
FACTS_SCOPES = frozenset({"read", "facts"})
HUMAN_SCOPE = "human"
HUMAN_SCOPES = frozenset({HUMAN_SCOPE})
HUMAN_BOT_SCOPE = "human_bot"
HUMAN_BOT_SCOPES = frozenset({HUMAN_BOT_SCOPE})
# Скоупы «только ручки заявок»: токену с любым из них все прочие ручки ядра отвечают 403.
HUMAN_ONLY_SCOPES = frozenset({HUMAN_SCOPE, HUMAN_BOT_SCOPE})
TERM_SCOPE = "term"
TERM_SCOPES = frozenset({TERM_SCOPE})
LISTEN_SCOPE = "listen"
LISTEN_SCOPES = frozenset({LISTEN_SCOPE})
NOTIFY_SCOPE = "notify"
NOTIFY_SCOPES = frozenset({NOTIFY_SCOPE})
PATTERNS_EXT_SCOPE = "patterns_ext"
PATTERNS_EXT_SCOPES = frozenset({PATTERNS_EXT_SCOPE})
# Ф86-1 (C1.3): клиент сводки дня (`day-digest.py` сисадмина) — по образцу `patterns_ext`: ТОЛЬКО
# `background`, ровно `{digest}`, только «все проекты», без `attach`. Свой флаг `digest_ok`: такому токену —
# 403 везде, кроме ручек `/digest/*` (они же отвечают 403 куке и любому другому bearer).
DIGEST_SCOPE = "digest"
DIGEST_SCOPES = frozenset({DIGEST_SCOPE})


def human_only(principal) -> bool:
    """Ф54-1/Ф54-2: токен ответчика (`human`) или бота владельца (`human_bot`) — открыты только ручки заявок."""
    return any(principal.has_scope(scope) for scope in HUMAN_ONLY_SCOPES)


ATTACH_DENIED_NAMES = ("mcp:orchestrator",)
ATTACH_DENIED_PREFIXES = ("mcp:wake-",)  # старые токены дежурной (Ф57-2: новых нет, запрет остался)


def check_attach(*, name: str | None, kind: str, scopes) -> None:
    """Ф14-3 A1 + П8: можно ли токену скоуп `attach`. Отказ — `ValueError` с текстом: не `interactive` (в том
    числе `background` и UI-сессия), вместе с `orchestrate`/`runs`, имя
    оркестратора `mcp:orchestrator` или старой дежурной `mcp:wake-*`. 
    Граница — здесь, в ядре, а не по имени профиля в CLI."""
    if kind != "interactive":
        raise ValueError("scope 'attach' — только interactive-токену")
    if "orchestrate" in scopes or "runs" in scopes:
        raise ValueError("scope 'attach' не выдаётся вместе с orchestrate/runs")
    value = name or ""
    if value in ATTACH_DENIED_NAMES or value.startswith(ATTACH_DENIED_PREFIXES):
        raise ValueError("scope 'attach' не выдаётся дежурной и оркестратору")


class AttachError(Exception):
    """Ф14-3 A1: `attach` у этого токена не меняется. `status` — HTTP-код ответа (404 — нет такого
    bearer-токена, 409 — отозван, истёк или запрет `check_attach`)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def set_attach(conn, *, token_id: int, on: bool, now_ms: int) -> list[str]:
    """Ф14-3 A1: дописать/убрать `attach` у ЖИВОГО токена (не отозван, не истёк) без перевыпуска. Запреты —
    те же, что при создании (`check_attach`). Возвращает новые скоупы. Вызывается внутри `Db.write()`;
    действует со следующего запроса (`resolve_principal` читает скоупы из базы на каждом запросе)."""
    row = conn.execute(
        "SELECT name, kind, scopes, revoked, expires_ts FROM main.api_token WHERE id = ?", (token_id,)
    ).fetchone()
    if row is None:
        raise AttachError(404, "not found")
    name, kind, scopes_str, revoked, expires_ts = row
    if revoked or (expires_ts is not None and expires_ts <= now_ms):
        raise AttachError(409, "токен отозван или истёк")
    scopes = (scopes_str or "").split()
    narrow = HUMAN_ONLY_SCOPES | TERM_SCOPES | LISTEN_SCOPES | NOTIFY_SCOPES
    narrow |= PATTERNS_EXT_SCOPES | DIGEST_SCOPES
    if narrow & set(scopes):
        # Ф54-1 (П3), Ф54-2 (П5), Ф56-5 (К4), Ф60-1 (П7): токен ответчика, бота, ключ терминала и токен службы
        # записи — ровно один скоуп; других им не дописывают.
        raise AttachError(
            409, "scope 'attach' не выдаётся токену ответчика (human), бота (human_bot), term и listen"
        )
    if on:
        try:
            check_attach(name=name, kind=kind, scopes=scopes)
        except ValueError as exc:
            raise AttachError(409, str(exc)) from exc
        if "attach" not in scopes:
            scopes.append("attach")
    else:
        scopes = [scope for scope in scopes if scope != "attach"]
    conn.execute("UPDATE main.api_token SET scopes = ? WHERE id = ?", (" ".join(scopes), token_id))
    return scopes


class CreatedToken(NamedTuple):
    token: str  # значение — отдаётся один раз
    token_id: int


def create_api_token(
    conn,
    *,
    name: str,
    kind: str,
    scopes: list[str],
    project_ids: list[int] | None,
    expires_ts: int | None,
) -> CreatedToken:
    """Bearer-токен для внешних клиентов. Значение отдаётся один раз, в БД — только sha256. Возвращает и
    значение, и `id` строки (спека 7a, П6: `hub mcp setup` помнит id, чтобы потом отозвать прежний токен).

    Отвергается (`ValueError` -> 422 на `/admin/tokens`, спека 7a, A2): неизвестный скоуп и `admin`;
    `background` + `send` (раздел «Principal и скоуп» спеки 4) и `background` + `organize`; `organize` у
    токена со списком проектов (раскладка меняет проект канала — токен проекта не должен ни видеть
    «Неразобранное», ни уводить канал из своего скоупа). `background` + `draft` разрешён. `work_host`
    (Ф9-1) — только `background`, все проекты, ровно `{work_host, read}`; `transcribe` (Ф14-6, П7) — только
    `background`, все проекты, ровно `{read, transcribe}`; `facts` (Ф24-1) — так же, ровно `{read, facts}`;
    `human` (Ф54-1, П3) — ровно `{human}`, проекты — любые или список; `human_bot` (Ф54-2, П5) — только
    `background`, ровно `{human_bot}`, только все проекты; `term` (Ф56-5, К4) — так же, ровно `{term}`;
    `listen` (Ф60-1, П7) — так же, ровно `{listen}`.
    """
    if kind not in ("interactive", "background"):
        raise ValueError(f"create_api_token: kind должен быть interactive|background, получено {kind!r}")
    unknown = sorted(set(scopes) - BEARER_SCOPES)
    if unknown:
        raise ValueError(f"create_api_token: скоупы не выдаются bearer-токену: {', '.join(unknown)}")
    if kind == "background" and "send" in scopes:
        raise ValueError("create_api_token: background-токену scope 'send' не выдаётся")
    if kind == "background" and "organize" in scopes:
        raise ValueError("create_api_token: background-токену scope 'organize' не выдаётся")
    if "organize" in scopes and project_ids is not None:
        raise ValueError("create_api_token: scope 'organize' — только у токена на все проекты")
    if "work_host" in scopes and (
        kind != "background" or project_ids is not None or frozenset(scopes) != WORK_HOST_SCOPES
    ):
        raise ValueError(
            "create_api_token: scope 'work_host' — только background-токен на все проекты "
            "со скоупами work_host read"
        )
    if "transcribe" in scopes and (
        kind != "background" or project_ids is not None or frozenset(scopes) != TRANSCRIBE_SCOPES
    ):
        raise ValueError(
            "create_api_token: scope 'transcribe' — только background-токен на все проекты "
            "со скоупами read transcribe"
        )
    if "facts" in scopes and (
        kind != "background" or project_ids is not None or frozenset(scopes) != FACTS_SCOPES
    ):
        raise ValueError(
            "create_api_token: scope 'facts' — только background-токен на все проекты со скоупами read facts"
        )
    if HUMAN_SCOPE in scopes and (kind != "background" or frozenset(scopes) != HUMAN_SCOPES):
        raise ValueError(
            "create_api_token: scope 'human' — отдельный background-токен ровно со скоупом human "
            "(без read и draft)"
        )
    if HUMAN_BOT_SCOPE in scopes and (
        kind != "background" or project_ids is not None or frozenset(scopes) != HUMAN_BOT_SCOPES
    ):
        raise ValueError(
            "create_api_token: scope 'human_bot' — отдельный background-токен на все проекты ровно со "
            "скоупом human_bot"
        )
    if TERM_SCOPE in scopes and (
        kind != "background" or project_ids is not None or frozenset(scopes) != TERM_SCOPES
    ):
        raise ValueError(
            "create_api_token: scope 'term' — отдельный background-токен на все проекты ровно со скоупом term"
        )
    if LISTEN_SCOPE in scopes and (
        kind != "background" or project_ids is not None or frozenset(scopes) != LISTEN_SCOPES
    ):
        raise ValueError(
            "create_api_token: scope 'listen' — отдельный background-токен на все проекты ровно со скоупом "
            "listen"
        )

    if NOTIFY_SCOPE in scopes and (
        kind != "background" or project_ids is not None or frozenset(scopes) != NOTIFY_SCOPES
    ):
        raise ValueError(
            "create_api_token: scope 'notify' — отдельный background-токен на все проекты ровно со скоупом "
            "notify"
        )

    if PATTERNS_EXT_SCOPE in scopes and (
        kind != "background" or project_ids is not None or frozenset(scopes) != PATTERNS_EXT_SCOPES
    ):
        raise ValueError(
            "create_api_token: scope 'patterns_ext' — отдельный background-токен на все проекты ровно со "
            "скоупом patterns_ext"
        )

    if DIGEST_SCOPE in scopes and (
        kind != "background" or project_ids is not None or frozenset(scopes) != DIGEST_SCOPES
    ):
        raise ValueError(
            "create_api_token: scope 'digest' — отдельный background-токен на все проекты ровно со скоупом "
            "digest"
        )

    if "attach" in scopes:
        try:
            check_attach(name=name, kind=kind, scopes=scopes)
        except ValueError as exc:
            raise ValueError(f"create_api_token: {exc}") from exc

    token = BEARER_PREFIX + secrets.token_urlsafe(32)
    project_scope_json = json.dumps(sorted(project_ids)) if project_ids is not None else None
    cur = conn.execute(
        "INSERT INTO main.api_token (name, kind, hash, scopes, project_scope_json, expires_ts, revoked) "
        "VALUES (?, ?, ?, ?, ?, ?, 0)",
        (name, kind, _sha256(token), " ".join(scopes), project_scope_json, expires_ts),
    )
    return CreatedToken(token, cur.lastrowid)


def revoke_api_token(conn, token_id: int) -> bool:
    """Спека 7a, A6: отозвать bearer-токен. UI-сессии этой функцией не отзываются (для них — `logout`):
    `False`, если такого bearer-токена нет. Отозванный токен получает 401 на следующем же запросе
    (`resolve_principal` проверяет `revoked=0` на каждом запросе)."""
    cur = conn.execute(
        "UPDATE main.api_token SET revoked = 1 WHERE id = ? AND kind IN ('interactive', 'background')",
        (token_id,),
    )
    return cur.rowcount > 0


def _principal_from_row(row) -> Principal:
    token_id, kind, scopes_str, project_scope_json = row
    scopes = frozenset((scopes_str or "").split())
    project_ids = frozenset(json.loads(project_scope_json)) if project_scope_json is not None else None
    return Principal(token_id=token_id, kind=kind, scopes=scopes, project_ids=project_ids)


class ResolveOutcome(NamedTuple):
    principal: Principal | None
    csrf: str | None  # только для cookie-принципала — вычисляется из значения cookie
    throttled: bool  # троттлинг сработал ДО проверки токена — bearer-ветка не оценивалась вовсе


def resolve_principal(
    conn,
    *,
    authorization: str | None,
    cookie_value: str | None,
    throttle: Throttle,
    now_ms: int,
    remote: bool = False,
) -> ResolveOutcome:
    """Определить Principal по заголовкам одного запроса. Синхронная функция — вызывается на read-only
    соединении (`db.reader()`), без записи; неудачные bearer-попытки только двигают троттлинг в памяти,
    в audit они, в отличие от логин-кода, не пишутся (спека явно требует audit только для логин-кода).

    Ф10-7, решение 6: `remote` — запрос пришёл на удалённый слушатель (туннель с телефона). Там принимается
    только UI-сессия `via='remote'`, на локальном — только `via='local'` или NULL (старые); bearer на
    удалённом не принимается вовсе (middleware режет `Authorization` раньше, это вторая линия).
    """
    is_bearer = authorization is not None and authorization.lower().startswith("bearer ")
    if is_bearer and cookie_value is not None:
        raise AmbiguousPrincipal("Bearer и cookie одновременно")

    if is_bearer and remote:
        return ResolveOutcome(None, None, False)

    if is_bearer:
        token = authorization[len("Bearer ") :].strip()
        row = conn.execute(
            "SELECT id, kind, scopes, project_scope_json FROM main.api_token "
            "WHERE hash = ? AND kind IN ('interactive', 'background') AND revoked = 0 "
            "AND (expires_ts IS NULL OR expires_ts > ?)",
            (_sha256(token), now_ms),
        ).fetchone()
        if row is None:
            # Троттлинг бьёт ТОЛЬКО по неудачам: валидный токен проходит всегда. Иначе любой клиент десятью
            # мусорными запросами отключал бы всех остальных (находка критика F2). Перебор 256 бит и так пуст.
            if throttle.blocked("bearer", now_ms):
                return ResolveOutcome(None, None, True)
            throttle.record_failure("bearer", now_ms)
            return ResolveOutcome(None, None, False)
        return ResolveOutcome(_principal_from_row(row), None, False)

    if cookie_value is not None:
        via_clause = "via = 'remote'" if remote else "(via IS NULL OR via = 'local')"
        row = conn.execute(
            "SELECT id, kind, scopes, project_scope_json FROM main.api_token "  # noqa: S608 — via_clause фиксирован
            "WHERE hash = ? AND kind = 'ui' AND revoked = 0 AND (expires_ts IS NULL OR expires_ts > ?) "
            f"AND {via_clause}",
            (_sha256(cookie_value), now_ms),
        ).fetchone()
        if row is None:
            return ResolveOutcome(None, None, False)
        return ResolveOutcome(_principal_from_row(row), compute_csrf(cookie_value), False)

    return ResolveOutcome(None, None, False)


def request_is_remote(request: Request) -> bool:
    """Ф10-7, решение 1: пришёл ли запрос на удалённый слушатель. Пометку ставит `SecurityMiddleware` по
    ПРИНЯВШЕМУ сокету (`scope["server"]`), не по заголовкам. Нет пометки — отказ (закрыто по умолчанию)."""
    marker = request.scope.get("state", {}).get("hub_remote")
    if marker is None:
        raise HTTPException(403, "forbidden: unknown listener")
    return bool(marker)


def principal_dependency(
    required_scope: str | None = None,
    *,
    human_ok: bool = False,
    term_ok: bool = False,
    listen_ok: bool = False,
    notify_ok: bool = False,
    patterns_ok: bool = False,
    digest_ok: bool = False,
) -> Callable[[Request], Principal]:
    """Фабрика FastAPI-зависимости: читает Authorization/Cookie текущего запроса, резолвит Principal на
    read-only соединении и проверяет скоуп. `request.state.csrf` — csrf для cookie-принципала (для /auth/me).

    Здесь, а не в middleware.py, потому что резолву нужна БД (поиск токена по хешу) — Host/Origin/CSRF —
    заголовок в middleware.py чисто синтаксический, без обращения к БД.

    Ф54-1 (П3): токену со скоупом `human` (ответчик заявок «нужен человек») — 403 на любой ручке, кроме тех,
    что объявили `human_ok` (ручки заявок): запрет структурный, а не по списку ручек. Ф54-2 (П5): то же —
    токену бота владельца `human_bot`. Ф56-5 (К4): то же — ключу терминала `term` везде, кроме ручек с `term_ok`
    (флаг свой: ручки заявок `human_ok` ключ терминала не пускают, ручки терминала — токены заявок). Ф60-1
    (П7): то же — токену службы записи `listen` везде, кроме ручек с `listen_ok` (`/listen/*`).
    Ф33-2: то же — спутнику уведомлений `notify` везде, кроме `/notify/wait` (`notify_ok`).
    Ф76-1: то же — сборщику внешней истории `patterns_ext` везде, кроме ручки приёма (`patterns_ok`).
    Ф86-1: то же — клиенту сводки дня `digest` везде, кроме ручек `/digest/*` (`digest_ok`).
    """

    async def _dependency(request: Request) -> Principal:
        db = request.app.state.db
        throttle = request.app.state.throttle
        clock = request.app.state.clock
        now_ms = int(clock() * 1000)
        remote = request_is_remote(request)

        authorization = request.headers.get("authorization")
        cookie_value = extract_cookie("; ".join(request.headers.getlist("cookie")) or None)

        try:
            with db.reader() as conn:
                outcome = resolve_principal(
                    conn,
                    authorization=authorization,
                    cookie_value=cookie_value,
                    throttle=throttle,
                    now_ms=now_ms,
                    remote=remote,
                )
        except AmbiguousPrincipal as exc:
            raise HTTPException(400, "ambiguous principal: bearer and cookie together") from exc

        if outcome.throttled:
            raise HTTPException(429, "too many attempts, try later")
        if outcome.principal is None:
            raise HTTPException(401, "unauthorized")
        if required_scope is not None and not outcome.principal.has_scope(required_scope):
            raise HTTPException(403, "forbidden")
        if not human_ok and human_only(outcome.principal):
            raise HTTPException(403, "forbidden")
        if not term_ok and outcome.principal.has_scope(TERM_SCOPE):
            raise HTTPException(403, "forbidden")
        if not listen_ok and outcome.principal.has_scope(LISTEN_SCOPE):
            raise HTTPException(403, "forbidden")
        if not notify_ok and outcome.principal.has_scope(NOTIFY_SCOPE):
            raise HTTPException(403, "forbidden")
        if not patterns_ok and outcome.principal.has_scope(PATTERNS_EXT_SCOPE):
            raise HTTPException(403, "forbidden")
        if not digest_ok and outcome.principal.has_scope(DIGEST_SCOPE):
            raise HTTPException(403, "forbidden")

        request.state.csrf = outcome.csrf
        return outcome.principal

    return _dependency


def principal_any_scope(*scopes: str) -> Callable[[Request], Principal]:
    """Спека 7a, A2: зависимость «любой из скоупов» (`POST /messages/send` принимает `send` ИЛИ `draft`).
    Резолв — тот же `principal_dependency()`, проверка скоупа — здесь; нет ни одного -> 403."""
    base = principal_dependency(human_ok=bool(HUMAN_ONLY_SCOPES & set(scopes)))

    async def _dependency(request: Request) -> Principal:
        principal = await base(request)
        if not any(principal.has_scope(scope) for scope in scopes):
            raise HTTPException(403, "forbidden")
        return principal

    return _dependency
