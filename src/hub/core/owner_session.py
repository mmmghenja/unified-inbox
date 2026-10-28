"""Сессия карточки канала владельца для его агента (Ф68-1, docs/architecture.md).

`GET /owner_channel/{ref}/session` — роль, файл роли и хвост ленты сессии хаба, чью карточку решает агент. Только
чтение (`db.reader()`), без записи; лента — тем же `held_work.list_events`, что окно сессии; файл роли — тем
же `roles.read_file`, что хост и окно «Инструкция роли» (путь — от рабочей папки проекта и у сессии в
подпапке, Ф50-7). Тексты ленты — вывод модели и инструментов сессии, чужие данные: через ту же маскировку,
что у карточек канала (`work.session.mask_secrets`).

Сессия карточки — `session_id` карточки очереди (`owner_queue._collect`: тот же расчёт «ref -> сессия
хаба», что у ответа по карточке). Карточки без сессии хаба (внешние сессии, черновики без автора-сессии
хаба, заявки без `hub_session_id`) — `{"session": null}`.

Ф66-2 (docs/architecture.md.2–A.3): то же содержимое по id сессии хаба
(`GET /owner_channel/sessions/{id}/context`, ветка разговора с агентом) — общие `read_context` и `finish`; ветка
реплики по `ref` карточки — `session_of_ref` (и у решённой карточки: `owner_queue._locate`)."""

from typing import NamedTuple

from .. import roles as roles_mod
from . import held as held_mod
from . import held_work as held_work_mod
from . import human_requests as human_mod
from . import owner_queue as zq

LIMIT_DEFAULT = 12
LIMIT_MAX = 40
BRIEF_MAX = 600  # строка события — как в ленте окна сессии
ANSWER_MAX = 4000  # последний ответ модели
ROLE_TEXT_MAX = 4000
TOTAL_MAX = 12_000  # общий потолок текста ответа
BRIEF_MIN = 80  # до скольких знаков укорачиваются строки старых событий при превышении потолка
_BEFORE_ALL = 2**62


class Found(NamedTuple):
    """Карточка нашлась и открыта: сессия хаба (`None` — у карточки её нет)."""

    session: held_mod.HubSession | None


def _mask(text) -> str:
    from ..work import session as work_session  # как в `owner_queue._mask`: код хоста, только stdlib

    return work_session.mask_secrets(str(text))


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def find(conn, ref: str, *, principal, now_ms: int, presence: list[dict]) -> Found:
    """Открытая карточка очереди по `ref` -> её сессия хаба. Нет, закрыта или другого проекта токена — 404
    (`OwnerError`); агенту карточка не в канале — 403 `not_in_channel`, как у ответа по карточке."""
    zq.parse_ref(ref)  # чужая форма `ref` — 404 раньше сборки очереди
    card = next(
        (
            c
            for c in zq._collect(conn, principal, now_ms=now_ms, presence=presence)  # noqa: SLF001
            if c["ref"] == ref
        ),
        None,
    )
    if card is None:
        raise zq.OwnerError(404, "not found")
    if zq.is_agent(principal) and not (card["in_channel"] or zq.errand_on(conn, ref)):  # Ф66-1 П2
        raise zq.OwnerError(403, zq.NOT_IN_CHANNEL)
    hub_id = card.get("session_id")
    if hub_id is None:
        return Found(None)
    rec = held_mod.get(conn, hub_id)
    if rec is None or rec.project_id != card["project_id"]:
        return Found(None)
    return Found(rec)


def session_of_ref(conn, ref, *, now_ms: int) -> int | None:
    """Ф66-2 (A.2): id сессии хаба карточки очереди по `ref` — и у решённой карточки (тот же расчёт, что у
    ответа по карточке, `owner_queue._locate`); чужая форма `ref`, нет карточки или у неё нет сессии хаба —
    `None`. Только чтение."""
    if not isinstance(ref, str) or not ref:
        return None
    try:
        prefix, number = zq.parse_ref(ref)
        loc = zq._locate(conn, prefix, number, now_ms, ref)  # noqa: SLF001 — тот же расчёт, что у ответа
    except zq.OwnerError:
        return None
    if loc is None or loc.hub_id is None:
        return None
    rec = held_mod.get(conn, loc.hub_id)
    if rec is None or (loc.project_id is not None and rec.project_id != loc.project_id):
        return None
    return rec.id


def by_id(conn, session_id: int, *, principal) -> held_mod.HubSession:
    """Ф66-2 (A.3): сессия хаба по id для куки и агента владельца (и закрытая). Нет такой или проект вне скоупа
    токена — 404."""
    rec = held_mod.get(conn, session_id)
    if rec is None or not zq._visible(principal, rec.project_id):  # noqa: SLF001
        raise zq.OwnerError(404, "not found")
    return rec


def read_context(conn, rec: held_mod.HubSession, *, before: int | None, limit: int) -> dict:
    """Чтение из базы для ответа «сессия»: `{session, events, more_before, last_answer}`; файл роли и
    потолок добавляет `finish` (вне транзакции)."""
    return {"session": session_view(rec), **tail(conn, rec, before=before, limit=limit)}


def window(conn, rec: held_mod.HubSession) -> dict | None:
    """Ф76-5: окно браузера сессии карточки (её последняя заявка «нужен человек» с профилем) — агент
    проходит в нём просьбу хода «нажми / отметь в окне» сам; нет — `None`."""
    return human_mod.last_window(conn, rec.project_id, rec.role)


def finish(reply: dict, rec: held_mod.HubSession) -> dict:
    """Файл роли (файловая система — звать через `asyncio.to_thread`) и общий потолок текста."""
    reply["role_file"] = role_file(rec)
    return cap(reply)


def _line(event: dict) -> str:
    """Строка события, как в ленте окна сессии: у шага инструмента — «инструмент: что», иначе — сводка."""
    summary = str(event.get("summary") or "")
    tool = event.get("tool")
    if tool and summary and summary != tool:
        return f"{tool}: {summary}"
    return summary or str(tool or "")


def _last_text(conn, rec: held_mod.HubSession) -> dict | None:
    """Последнее событие `text` (ответ модели) сессии — с полным текстом (`summary` до 8000)."""
    if rec.store is None:
        return None
    row = conn.execute(
        held_work_mod._sql(  # noqa: SLF001 — тот же приём, что у `held_work.list_events`
            rec.store,
            "SELECT {cols} FROM {schema}.held_event WHERE session_id = ? AND kind = 'text' "
            "ORDER BY id DESC LIMIT 1",
            cols=held_work_mod._EVENT_LIST_COLS,  # noqa: SLF001
        ),
        (rec.id,),
    ).fetchone()
    return held_work_mod._event_dict(row) if row is not None else None  # noqa: SLF001


def tail(conn, rec: held_mod.HubSession, *, before: int | None, limit: int) -> dict:
    """Хвост ленты: последние `limit` событий до `before` (без него — с конца), по возрастанию `id`.
    `{events, more_before, last_answer}`; у последнего ответа модели (только на странице с конца) — `text`
    до `ANSWER_MAX`; если он раньше отданных событий — отдельным `last_answer`."""
    rows = held_work_mod.list_events(
        conn, rec, after=None, before=before if before is not None else _BEFORE_ALL, limit=limit + 1
    )
    more = len(rows) > limit
    rows = rows[-limit:] if more else rows
    events = [
        {"id": e["id"], "ts": e["ts"], "kind": e["kind"], "brief": _clip(_mask(_line(e)), BRIEF_MAX)}
        for e in rows
    ]
    last_answer = None
    if before is None:
        last = _last_text(conn, rec)
        if last is not None:
            text = _clip(_mask(last.get("summary") or ""), ANSWER_MAX)
            own = next((e for e in events if e["id"] == last["id"]), None)
            if own is not None:
                own["text"] = text
            else:
                last_answer = {"id": last["id"], "ts": last["ts"], "text": text}
    return {
        "events": events,
        "more_before": events[0]["id"] if more and events else None,
        "last_answer": last_answer,
    }


def role_file(rec: held_mod.HubSession) -> dict | None:
    """Файл роли сессии (`null` — у сессии его нет): `{path, text, truncated}` или `{path, error}`. Читает
    файловую систему — звать вне транзакции (`asyncio.to_thread`)."""
    if not rec.role_file:
        return None
    loaded = roles_mod.read_file(rec.workdir, rec.role_file)
    if loaded.text is None:
        return {"path": rec.role_file, "error": loaded.reason}
    text = _mask(loaded.text)
    return {"path": rec.role_file, "text": _clip(text, ROLE_TEXT_MAX), "truncated": len(text) > ROLE_TEXT_MAX}


def session_view(rec: held_mod.HubSession) -> dict:
    return {
        "id": rec.id,
        "project_id": rec.project_id,
        "role": rec.role,
        "number": rec.role_no,
        "mode": rec.mode,
        "engine": rec.engine,
        "title": rec.title,
    }


def _total(reply: dict) -> int:
    size = sum(len(e["brief"]) + len(e.get("text") or "") for e in reply["events"])
    if reply.get("last_answer"):
        size += len(reply["last_answer"]["text"])
    role = reply.get("role_file")
    if isinstance(role, dict) and isinstance(role.get("text"), str):
        size += len(role["text"])
    return size


def cap(reply: dict) -> dict:
    """Общий потолок `TOTAL_MAX`: при превышении строки событий укорачиваются от старых к новым до
    `BRIEF_MIN` (40 строк по 80 знаков + ответ и файл роли по 4000 — в потолок укладываются всегда)."""
    over = _total(reply) - TOTAL_MAX
    for event in reply["events"]:
        if over <= 0:
            break
        brief = event["brief"]
        if len(brief) <= BRIEF_MIN:
            continue
        keep = max(BRIEF_MIN, len(brief) - over)
        event["brief"] = _clip(brief, keep)
        over -= len(brief) - len(event["brief"])
    return reply
