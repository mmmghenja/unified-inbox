"""Вход/выход/принципал. Спека, раздел «Вход UI»: одноразовый код -> cookie, CSRF из cookie, logout=revoke.

Ф10-7 (docs/architecture.md–7): вход кодом — только локальный слушатель, вход
паролем — только удалённый (туннель с телефона); продление `POST /auth/renew`; список и отзыв UI-сессий.

Маршруты не пишут SQL: запись — через `security`/`remote` внутри `Db.write()`, чтение принципала — через
`security.principal_dependency()` (read-only соединение).
"""

import asyncio
import math

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import remote as remote_mod
from ..security import (
    UI_SESSION_TTL_MS,
    Principal,
    extract_cookie,
    list_ui_sessions,
    login,
    login_code_valid,
    logout,
    principal_dependency,
    renew_ui_session,
    request_is_remote,
    revoke_all_ui_sessions,
    revoke_ui_session,
    ui_session_renewal,
)

router = APIRouter(prefix="/auth", tags=["auth"])

COOKIE_NAME = "hub_session"
_authed = Depends(principal_dependency())


def _now_ms(request: Request) -> int:
    return int(request.app.state.clock() * 1000)


def _request_cookie(request: Request) -> str | None:
    return extract_cookie("; ".join(request.headers.getlist("cookie")) or None)


def _is_https(request: Request) -> bool:
    """Ф14-9 решение 6 и П5: запрос пришёл по https (TLS удалённого слушателя; `proxy_headers` выключены —
    схему даёт только принявший сокет)."""
    return request.url.scheme == "https"


def _set_session_cookie(response: Response, token: str, *, secure: bool) -> None:
    """Решение 7 Ф10-7: одна и та же кука и на локальном, и на удалённом слушателе — без `Domain` (только
    свой хост: удалённая не уходит на соседние сервисы домена). Ф14-9 П5: `Secure` — когда запрос пришёл по
    https (телефон через TLS удалённого слушателя); по http (локальный 127.0.0.1) — без него."""
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/",
        # Срок куки = срок сессии на сервере (30 дней): без него кука сессионная и умирает с закрытием
        # браузера — закладка на 127.0.0.1:8787 требовала бы `hub ui` из терминала каждый раз (просьба владельца
        # 23.09). Угроза «процесс того же uid читает профиль браузера» принята планом (раздел 7) и так.
        max_age=UI_SESSION_TTL_MS // 1000,
    )


def _require_ui(principal: Principal) -> None:
    if not principal.is_ui:
        raise HTTPException(403, "forbidden: ui session required")


class SessionIn(BaseModel):
    code: str


@router.post("/session")
async def create_session(body: SessionIn, request: Request, response: Response) -> dict:
    if request_is_remote(request):
        # Код выдаёт только `hub ui` на ноутбуке — на удалённом слушателе вход кодом не принимается.
        raise HTTPException(403, "forbidden: local only")
    db = request.app.state.db
    throttle = request.app.state.throttle
    now_ms = _now_ms(request)

    if throttle.blocked("login", now_ms):
        # Под троттлингом верный код всё равно проходит — иначе любой, кто достучался до ручки, запирал бы
        # хозяину вход (находка критика F2); неверные отбиваются здесь, без записи в БД.
        with db.reader() as conn:
            valid = login_code_valid(conn, code=body.code, now_ms=now_ms)
        if not valid:
            raise HTTPException(429, "too many attempts, try later")
    user_agent = request.headers.get("user-agent")
    prev_cookie = _request_cookie(request)
    result = await db.write(
        lambda conn: login(
            conn, code=body.code, now_ms=now_ms, user_agent=user_agent, prev_cookie=prev_cookie
        )
    )
    if not result.ok:
        throttle.record_failure("login", now_ms)
        raise HTTPException(401, "invalid or expired code")

    _set_session_cookie(response, result.token, secure=_is_https(request))
    return {"csrf": result.csrf, "expires_ts": result.expires_ts}


class PasswordIn(BaseModel):
    password: str


def _too_many(retry_after_s: int) -> JSONResponse:
    retry_after_s = max(1, retry_after_s)
    return JSONResponse(
        {"detail": "too many attempts", "retry_after_s": retry_after_s},
        status_code=429,
        headers={"Retry-After": str(retry_after_s)},
    )


@router.post("/password")
async def create_session_by_password(body: PasswordIn, request: Request, response: Response):
    """Вход паролем с телефона (решения 3–4, П5, П6). Только удалённый слушатель. Попытки сериализованы
    замком процесса; больше 3 ожидающих -> 429 сразу; под блоком — 429 БЕЗ проверки пароля (верный тоже).
    Отказ под блоком и из-за очереди в БД не пишется; в audit — `ok`, `invalid` и `blocked` в начале блока."""
    if not request_is_remote(request):
        raise HTTPException(403, "forbidden: remote only")
    state = request.app.state.remote
    db = request.app.state.db

    if state.pw_waiting >= remote_mod.QUEUE_MAX_WAITING:
        return _too_many(1)
    state.pw_waiting += 1
    try:
        await state.pw_lock.acquire()
    finally:
        state.pw_waiting -= 1
    try:
        now_ms = _now_ms(request)
        with db.reader() as conn:
            config = remote_mod.read_config(conn)
        if not config.enabled:
            raise HTTPException(421, "remote access disabled")
        if config.password is None:
            raise HTTPException(403, "forbidden: password not set")
        blocked_ms = remote_mod.blocked_for_ms(config, now_ms)
        if blocked_ms:
            return _too_many(math.ceil(blocked_ms / 1000))

        stored = config.password
        if len(body.password) > remote_mod.PASSWORD_MAX_LEN:
            ok = False  # длиннее допустимого пароль быть не может — scrypt на мегабайте не гоняем
        else:
            ok = await asyncio.to_thread(remote_mod.verify_password, body.password, stored)

        now_ms = _now_ms(request)
        if not ok:
            await db.write(lambda conn: remote_mod.record_failure(conn, now_ms=now_ms))
            raise HTTPException(401, "invalid password")

        user_agent = request.headers.get("user-agent")
        prev_cookie = _request_cookie(request)
        result = await db.write(
            lambda conn: remote_mod.complete_password_login(
                conn, checked_password=stored, now_ms=now_ms, user_agent=user_agent, prev_cookie=prev_cookie
            )
        )
    finally:
        state.pw_lock.release()

    if result.outcome == "disabled":
        raise HTTPException(421, "remote access disabled")
    if result.outcome != "ok":
        raise HTTPException(401, "invalid password")
    _set_session_cookie(response, result.token, secure=_is_https(request))
    return {"csrf": result.csrf, "expires_ts": result.expires_ts}


@router.get("/me")
async def get_me(request: Request, principal=_authed) -> dict:
    return {
        "token_id": principal.token_id,
        "kind": principal.kind,
        "scopes": sorted(principal.scopes),
        "project_ids": sorted(principal.project_ids) if principal.project_ids is not None else None,
        "csrf": getattr(request.state, "csrf", None),
        "remote": request_is_remote(request),
    }


@router.post("/logout")
async def post_logout(request: Request, response: Response, principal=_authed) -> dict:
    if not principal.is_ui:
        # Выход — действие UI-сессии; bearer одним ошибочным запросом отозвал бы сам себя (критик, F9).
        raise HTTPException(403, "forbidden: ui session required")
    db = request.app.state.db
    now_ms = _now_ms(request)
    await db.write(lambda conn: logout(conn, token_id=principal.token_id, now_ms=now_ms))

    if _request_cookie(request) is not None:
        response.delete_cookie(COOKIE_NAME, path="/", secure=_is_https(request))
    return {"ok": True}


@router.post("/renew")
async def post_renew(request: Request, response: Response, principal=_authed) -> dict:
    """Решение 6: продление при использовании — `expires_ts = now + 30 дней`; чаще раза в 10 минут без записи
    (ответ тот же). Кука выставляется заново с тем же значением и `Max-Age` 30 дней."""
    _require_ui(principal)
    db = request.app.state.db
    now_ms = _now_ms(request)
    with db.reader() as conn:
        expires_ts, due = ui_session_renewal(conn, token_id=principal.token_id, now_ms=now_ms)
    if due:
        renewed = await db.write(
            lambda conn: renew_ui_session(conn, token_id=principal.token_id, now_ms=now_ms)
        )
        if renewed is None:
            raise HTTPException(401, "unauthorized")
        expires_ts = renewed
    cookie_value = _request_cookie(request)
    if cookie_value is not None:
        _set_session_cookie(response, cookie_value, secure=_is_https(request))
    return {"expires_ts": expires_ts}


@router.get("/sessions")
async def get_sessions(request: Request, principal=_authed) -> dict:
    _require_ui(principal)
    with request.app.state.db.reader() as conn:
        sessions = list_ui_sessions(conn, now_ms=_now_ms(request), current_id=principal.token_id)
    return {"sessions": sessions}


@router.post("/sessions/{session_id}/revoke")
async def post_session_revoke(
    session_id: int, request: Request, response: Response, principal=_authed
) -> dict:
    _require_ui(principal)
    db = request.app.state.db
    now_ms = _now_ms(request)
    ok = await db.write(
        lambda conn: revoke_ui_session(
            conn, session_id=session_id, now_ms=now_ms, actor_token_id=principal.token_id
        )
    )
    if not ok:
        raise HTTPException(404, "not found")
    if session_id == principal.token_id:
        response.delete_cookie(COOKIE_NAME, path="/", secure=_is_https(request))
    return {"ok": True}


class RevokeAllIn(BaseModel):
    keep_current: bool


@router.post("/sessions/revoke-all")
async def post_sessions_revoke_all(
    body: RevokeAllIn, request: Request, response: Response, principal=_authed
) -> dict:
    _require_ui(principal)
    db = request.app.state.db
    now_ms = _now_ms(request)
    keep_id = principal.token_id if body.keep_current else None
    revoked = await db.write(
        lambda conn: revoke_all_ui_sessions(
            conn, keep_id=keep_id, now_ms=now_ms, actor_token_id=principal.token_id
        )
    )
    if not body.keep_current:
        response.delete_cookie(COOKIE_NAME, path="/", secure=_is_https(request))
    return {"revoked": revoked}
