"""Ручки правил проекта для входящих (Ф63-1, docs/architecture.md и «Контракт
ручек 63-1 (факт)» в конце спеки).

- `GET /projects/{id}/inbound-rules` — UI-сессия (любой проект) или bearer со скоупом `read`, проекту видимый.
- `POST /projects/{id}/inbound-rules`, `PATCH|DELETE /projects/{id}/inbound-rules/{rule_id}` — UI-сессия
  (CSRF — middleware) или bearer СЕССИИ с живой подпиской на ЭТОТ проект и ролью (тело несёт `session`, как у
  `hub_remind`; токен — тот, которому положена подписка: `_subscriber_error`). Аудит `inbound_rule_add|update|
  remove`. Маршруты не пишут SQL: `core/inbound_rules.py` внутри `db.write()`/`db.reader()`.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from .. import inbound_rules as rules_mod
from .. import subs as subs_mod
from ..security import Principal, principal_dependency
from .subs import SessionIn, _subscriber_error
from .write import _client_name, _new_request_id, _now_ms

public_router = APIRouter(tags=["inbound-rules"])

_any = Depends(principal_dependency())
_read = Depends(principal_dependency("read"))

_RAW_LIST_MAX = 100  # до нормализации; предел правила (20) проверяет `inbound_rules.normalize`
_RAW_ITEM_MAX = 400
_PROJECT_MAX = 2**53


class RuleIn(BaseModel):
    """`POST`: новое правило. Поля условия и действий — `inbound_rules.normalize`."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    title: StrictStr = Field(max_length=_RAW_ITEM_MAX)
    accounts: list[StrictInt] | None = Field(default=None, max_length=_RAW_LIST_MAX)
    from_: list[StrictStr] | None = Field(default=None, alias="from", max_length=_RAW_LIST_MAX)
    subject: list[StrictStr] | None = Field(default=None, max_length=_RAW_LIST_MAX)
    text: list[StrictStr] | None = Field(default=None, max_length=_RAW_LIST_MAX)
    exclude: list[StrictStr] | None = Field(default=None, max_length=_RAW_LIST_MAX)
    kinds: list[StrictStr] | None = Field(default=None, max_length=_RAW_LIST_MAX)
    wake: list[StrictStr] | None = Field(default=None, max_length=_RAW_LIST_MAX)
    attention: Literal["react", "info"] | None = None
    archive: StrictBool | None = None
    enabled: StrictBool = True
    session: SessionIn | None = None

    def fields(self) -> dict:
        out: dict = {}
        for name in self.model_fields_set | {"title", "enabled"}:
            if name == "session":
                continue
            out["from" if name == "from_" else name] = getattr(self, name)
        return out


class RulePatchIn(BaseModel):
    """`PATCH`: только заданные поля; список `null` или `[]` — очистить поле, `attention: null` — снять."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    title: StrictStr | None = Field(default=None, max_length=_RAW_ITEM_MAX)
    accounts: list[StrictInt] | None = Field(default=None, max_length=_RAW_LIST_MAX)
    from_: list[StrictStr] | None = Field(default=None, alias="from", max_length=_RAW_LIST_MAX)
    subject: list[StrictStr] | None = Field(default=None, max_length=_RAW_LIST_MAX)
    text: list[StrictStr] | None = Field(default=None, max_length=_RAW_LIST_MAX)
    exclude: list[StrictStr] | None = Field(default=None, max_length=_RAW_LIST_MAX)
    kinds: list[StrictStr] | None = Field(default=None, max_length=_RAW_LIST_MAX)
    wake: list[StrictStr] | None = Field(default=None, max_length=_RAW_LIST_MAX)
    attention: Literal["react", "info"] | None = None
    archive: StrictBool | None = None
    enabled: StrictBool | None = None
    session: SessionIn | None = None

    def fields(self) -> dict:
        out: dict = {}
        for name in self.model_fields_set:
            if name == "session":
                continue
            if name in ("title", "enabled") and getattr(self, name) is None:
                raise rules_mod.RuleError(422, f"{name}: null нельзя")
            out["from" if name == "from_" else name] = getattr(self, name)
        return out


class RuleRemoveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session: SessionIn | None = None


def _http(exc: rules_mod.RuleError) -> HTTPException:
    return HTTPException(exc.status, exc.detail)


def _sees(principal: Principal, project_id: int) -> bool:
    return principal.project_ids is None or project_id in principal.project_ids


def session_sub(conn, principal: Principal, session: dict | None, project_id: int) -> subs_mod.SubRec:
    """Право сессии на правила проекта: живая подписка ЭТОЙ сессии на ЭТОТ проект с ролью. Нет сессии в теле —
    422; процесс мёртв или подписок нет — 409 «сначала hub_subscribe»; подписка на другой проект — 403
    (только свой проект); без роли — 403."""
    if session is None:
        raise rules_mod.RuleError(422, "session — обязательна bearer-токену сессии")
    pid, proc_start, session_id = session["pid"], session["proc_start"], session["session_id"]
    if not subs_mod.is_alive(pid, proc_start, session.get("host")):
        raise rules_mod.RuleError(409, "сессия не найдена")
    rows = conn.execute(
        "SELECT id FROM main.session_sub WHERE api_token_id = ? AND session_pid = ? "
        "AND session_proc_start = ? AND session_id = ? AND ended_ts IS NULL ORDER BY id",
        (principal.token_id, pid, proc_start, session_id),
    ).fetchall()
    recs = [rec for (sub_id,) in rows if (rec := subs_mod.get_sub(conn, sub_id)) is not None]
    if not recs:
        raise rules_mod.RuleError(
            409, "сначала hub_subscribe(role=…) — правила входящих заводит сессия с подпиской на проект"
        )
    own = next((rec for rec in recs if rec.project_id == project_id), None)
    if own is None:
        raise rules_mod.RuleError(403, "правила входящих — только своего проекта (проекта подписки сессии)")
    if own.role_key is None:
        raise rules_mod.RuleError(403, "правила входящих заводит сессия с ролью: hub_subscribe(role=…)")
    return own


def _writer(conn, request: Request, principal: Principal, session, project_id: int, request_id: str) -> dict:
    """Кто пишет: UI-сессия владельца или подписанная сессия с ролью в этом проекте. `actor` — для аудита и
    строки правила."""
    actor = {
        "kind": principal.kind,
        "token_id": principal.token_id,
        "client_name": _client_name(request),
        "request_id": request_id,
    }
    if principal.is_ui:
        return actor
    error = _subscriber_error(conn, principal)
    if error is not None:
        raise rules_mod.RuleError(403, error)
    if not _sees(principal, project_id):
        raise rules_mod.RuleError(403, "проект не виден токену")
    sub = session_sub(conn, principal, session, project_id)
    actor["role"] = sub.role
    actor["sub_id"] = sub.id
    return actor


@public_router.get("/projects/{project_id}/inbound-rules")
async def get_inbound_rules(project_id: int, request: Request, principal: Principal = _read) -> dict:
    """Правила проекта (свежесть — по `id`). 404 — проекта нет или он не виден токену."""
    if not 1 <= project_id <= _PROJECT_MAX:
        raise HTTPException(404, "проекта нет")
    with request.app.state.db.reader() as conn:
        if not _sees(principal, project_id) or not rules_mod.project_exists(conn, project_id):
            raise HTTPException(404, "проекта нет")
        return {
            "project_id": project_id,
            "rules": rules_mod.list_rules(conn, project_id),
            "limits": {
                "rules": rules_mod.RULES_MAX,
                "list": rules_mod.LIST_MAX,
                "title": rules_mod.TITLE_MAX,
                "words": rules_mod.keywords_mod.RULE_WORDS_MAX,
            },
        }


async def _write(request: Request, response: Response, fn):
    db = request.app.state.db
    try:
        return await db.write(fn)
    except rules_mod.RuleError as exc:
        raise _http(exc) from exc


@public_router.post("/projects/{project_id}/inbound-rules", status_code=201)
async def post_inbound_rule(
    project_id: int, body: RuleIn, request: Request, response: Response, principal: Principal = _any
) -> dict:
    """Новое правило. 201 — строка правила. 403 — не UI и не сессия с ролью этого проекта; 404 — проекта нет;
    409 — сессия без подписки; 422 — поле (текст: поле, номер элемента, причина); 429 — 50 правил."""
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    session = body.session.model_dump() if body.session is not None else None

    def _fn(conn):
        actor = _writer(conn, request, principal, session, project_id, request_id)
        rec = rules_mod.add(conn, project_id, body.fields(), actor=actor, now_ms=now_ms)
        return rules_mod.rule_dict(conn, rec)

    return await _write(request, response, _fn)


@public_router.patch("/projects/{project_id}/inbound-rules/{rule_id}")
async def patch_inbound_rule(
    project_id: int,
    rule_id: int,
    body: RulePatchIn,
    request: Request,
    response: Response,
    principal: Principal = _any,
) -> dict:
    """Правка, выключить/включить (`enabled`). 404 — правила нет в этом проекте; прочие коды — как у POST."""
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    session = body.session.model_dump() if body.session is not None else None

    def _fn(conn):
        actor = _writer(conn, request, principal, session, project_id, request_id)
        rec = rules_mod.update(conn, project_id, rule_id, body.fields(), actor=actor, now_ms=now_ms)
        return rules_mod.rule_dict(conn, rec)

    return await _write(request, response, _fn)


@public_router.delete("/projects/{project_id}/inbound-rules/{rule_id}")
async def delete_inbound_rule(
    project_id: int,
    rule_id: int,
    request: Request,
    response: Response,
    body: RuleRemoveIn | None = None,
    principal: Principal = _any,
) -> dict:
    """Удалить правило. 200 — `{removed: id}`; 404 — правила нет в этом проекте; прочие коды — как у POST.
    Сообщения, на которых правило сработало, пометку сохраняют (название пропадает)."""
    now_ms = _now_ms(request)
    request_id = _new_request_id(response)
    session = body.session.model_dump() if body is not None and body.session is not None else None

    def _fn(conn):
        actor = _writer(conn, request, principal, session, project_id, request_id)
        rec = rules_mod.remove(conn, project_id, rule_id, actor=actor, now_ms=now_ms)
        return {"removed": rec.id, "project_id": project_id}

    return await _write(request, response, _fn)
