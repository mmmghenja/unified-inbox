"""«Разобрано сессией» (Ф37-3, docs/architecture.md).

Подписанная сессия проекта с ролью снимает входящее с владельца: беседа считается прочитанной до сообщения `up_to`
(уходит из «Нужна реакция» и «Ждёт»), открытые карточки дежурного закрываются, а у владельца остаётся строка в
свёрнутом блоке «Разобрано сессией» с пометкой сессии — её он убирает одним кликом. Отметка в ИСТОЧНИК
(`read_ack`) не ставится: «прочитано» собеседнику и на телефоне ставит только сам владелец.

Выключатель — ключ `session_handled` политики проекта (`agent_policy_json`), по умолчанию выключено; включает
владелец (`hub project set N --session-handled on` или `PATCH /projects/{id}`).

Все функции записи — только внутри `Db.write()`, время — параметром `now_ms`.
"""

import json

from . import agent_items, autosend, draft_channels
from . import channels as channels_mod
from . import threads as threads_mod
from .audit import write_audit
from .db import db_of

POLICY_KEY = "session_handled"
NOTE_MIN = 3
NOTE_MAX = 200
# B3: отметок в час на токен (считаются по журналу `channel_handled`: повторная отметка строку обновляет).
# 23.10: было 60 — сессии разбирают завалы пачками по 50 (Hooli, Initech) и упирались за одну пачку.
HANDLED_PER_HOUR = 1000
_HOUR_MS = 60 * 60 * 1000


class HandledError(Exception):
    """Отказ отметки: `status` — HTTP-код (403/404/422/429), `detail` — понятный текст без чужих данных."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


# --- выключатель проекта (A) -------------------------------------------------------------------------------


def _policy_dict(policy_json: str | None) -> dict:
    if not policy_json:
        return {}
    try:
        value = json.loads(policy_json)
    except ValueError, TypeError:
        return {}
    return value if isinstance(value, dict) else {}


def enabled(policy_json: str | None) -> bool:
    """A: `session_handled: true` в `project.agent_policy_json`; нет ключа или кривой — выключено."""
    return _policy_dict(policy_json).get(POLICY_KEY) is True


def project_enabled(conn, project_id: int | None) -> bool:
    if project_id is None:
        return False
    row = conn.execute("SELECT agent_policy_json FROM main.project WHERE id = ?", (project_id,)).fetchone()
    return row is not None and enabled(row[0])


def set_project_enabled(conn, project_id: int, on: bool) -> bool | None:
    """A: ключ `session_handled` политики проекта (прочие ключи не трогаются). Нет проекта — `None`."""
    row = conn.execute("SELECT agent_policy_json FROM main.project WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        return None
    policy = _policy_dict(row[0])
    policy[POLICY_KEY] = bool(on)
    conn.execute(
        "UPDATE main.project SET agent_policy_json = ? WHERE id = ?",
        (json.dumps(policy, ensure_ascii=False, sort_keys=True), project_id),
    )
    return bool(on)


# --- отметка сессии (B) ------------------------------------------------------------------------------------


def note_form(value: str | None) -> str:
    """B1: пометка — одна строка 3..200 символов после обрезки пробелов."""
    note = " ".join((value or "").split())
    if len(note) < NOTE_MIN:
        raise HandledError(422, f"note: что это было и почему не нужен владелец — от {NOTE_MIN} символов")
    if len(note) > NOTE_MAX:
        raise HandledError(422, f"note: не длиннее {NOTE_MAX} символов")
    return note


def _rate_count(conn, token_id: int, now_ms: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM main.audit WHERE action = 'channel_handled' AND ts > ? AND api_token_id = ? "
        "AND result IN ('changed', 'unchanged')",
        (now_ms - _HOUR_MS, token_id),
    ).fetchone()
    return row[0]


def mark(
    conn,
    *,
    principal,
    token_name: str | None,
    session,
    channel_id: int,
    project_id: int | None,
    up_to_message_id: int | str,
    note: str,
    now_ms: int,
    waiting=None,
    client_name: str | None = None,
    request_id: str = "",
) -> dict:
    """B1–B4: отметка «разобрано до сообщения» подписанной сессией проекта с ролью. Видимость беседы токену и
    её проект (`project_id`) проверяет маршрут в той же транзакции (`queries.channel_visible`).

    Условия B1 (все, иначе `HandledError`, ничего не пишется): подписанная сессия проекта беседы с ролью
    (`autosend.role_sub` — та же проверка, что у авто-отправки Ф37-1); у проекта включён `session_handled`;
    беседа не в архиве и не заготовка `new:…`; сообщение принадлежит беседе; пометка 3..200 символов; лимит
    B3.

    Действие B2: отметка прочтения до `up_to` — только вперёд (`channels.mark_read` для сообщения верхнего
    уровня, `threads.mark_thread_read` для ответа в треде), БЕЗ отметки в источник; открытые карточки
    дежурного беседы — `done`; строка `main.channel_handled` (B4: неубранная строка той же беседы и роли
    обновляется); аудит `channel_handled` без текста пометки."""
    try:
        sub = autosend.role_sub(
            conn,
            principal=principal,
            token_name=token_name,
            session=session,
            project_id=project_id,
            now_ms=now_ms,
            waiting=waiting,
        )
    except autosend.NoRoleSub as exc:
        raise HandledError(403, f"отметка «разобрано»: {exc.reason}") from exc
    if not project_enabled(conn, project_id):
        raise HandledError(
            403,
            "отметка «разобрано» в проекте выключена — включает владелец: "
            f"hub project set {project_id} --session-handled on",
        )
    text = note_form(note)
    schema = db_of(channel_id)
    channel = conn.execute(
        f"SELECT archived, ext_id FROM {schema}.channel WHERE id = ?",  # noqa: S608
        (channel_id,),
    ).fetchone()
    if channel is None:
        raise HandledError(404, "not found")
    if channel[0]:
        raise HandledError(422, "беседа в архиве")
    if draft_channels.is_temp_ext_id(channel[1]):
        raise HandledError(422, "новая беседа — отмечать нечего")
    if up_to_message_id == "last":
        # «До последнего»: самое позднее по порядку источника живое чужое сообщение ленты беседы (не ответ в
        # треде — тот двигал бы маркер треда, а не беседы; не по id — догон вставляет старое с бо́льшим id).
        top = threads_mod.top_level_sql("thread_ext_id", "ext_id", "thread_broadcast")
        last = conn.execute(
            f"SELECT id FROM {schema}.message "  # noqa: S608
            "WHERE channel_id = ? AND hidden = 0 AND is_me = 0 AND deleted = 0 AND ext_id IS NOT NULL "
            f"AND {top} ORDER BY ts DESC, ext_sort DESC, id DESC LIMIT 1",
            (channel_id,),
        ).fetchone()
        if last is None:
            raise HandledError(422, "up_to: в беседе нет чужого живого сообщения, до которого можно отметить")
        up_to_message_id = last[0]
    elif not isinstance(up_to_message_id, int) or isinstance(up_to_message_id, bool):
        raise HandledError(422, 'up_to: число (message_id из hub_read) или "last"')
    message = (
        conn.execute(
            "SELECT channel_id, ext_id, thread_ext_id, thread_broadcast, hidden, is_me, deleted, ts, "  # noqa: S608
            f"ext_sort FROM {schema}.message WHERE id = ?",
            (up_to_message_id,),
        ).fetchone()
        if db_of(up_to_message_id) == schema
        else None
    )
    if message is None or message[0] != channel_id or message[4]:
        raise HandledError(422, "up_to: сообщение не из этой беседы — возьми message_id из hub_read")
    if message[5] or message[6] or message[1] is None:
        # Ревью: своя заготовка черновика стоит «сейчас по часам хаба» — отметка по ней прочитала бы входящее,
        # которого сессия не видела (дошло позже со старым временем источника). Только чужое живое сообщение.
        raise HandledError(
            422, "up_to: нужно последнее ЧУЖОЕ сообщение, которое ты разобрал (не своё и не удалённое)"
        )
    if _rate_count(conn, principal.token_id, now_ms) >= HANDLED_PER_HOUR:
        raise HandledError(429, f"лимит отметок «разобрано» — {HANDLED_PER_HOUR} в час — исчерпан")

    _channel, ext_id, thread_ext_id, thread_broadcast, _hidden, _is_me, _deleted, msg_ts, msg_sort = message
    if threads_mod.is_reply(thread_ext_id, ext_id) and not thread_broadcast:
        # Ответ в треде двигает маркер ЭТОГО треда; маркер беседы не трогается.
        try:
            changed = threads_mod.mark_thread_read(
                conn, schema, channel_id, thread_ext_id, up_to_message_id=up_to_message_id
            )
        except threads_mod.ThreadError as exc:
            raise HandledError(422, "up_to: тред этого сообщения хабу неизвестен") from exc
    else:
        changed = channels_mod.mark_read(conn, channel_id, up_to_message_id=up_to_message_id, now_ms=now_ms)
    # Ревью: «разобрано до `up_to`» — карточка на более новом сообщении остаётся открытой.
    closed = agent_items.close_cards_up_to(
        conn, schema, channel_id, ts=msg_ts, ext_sort=msg_sort or 0, message_id=up_to_message_id
    )

    existing = conn.execute(
        "SELECT id FROM main.channel_handled WHERE channel_id = ? AND role_key = ? AND dismissed_ts IS NULL",
        (channel_id, sub.role_key),
    ).fetchone()
    if existing is not None:
        # B4: одна строка на беседу и роль, пока владелец её не убрал.
        handled_id = existing[0]
        conn.execute(
            "UPDATE main.channel_handled SET message_id = ?, note = ?, created_ts = ?, role = ?, sub_id = ?, "
            "project_id = ? WHERE id = ?",
            (up_to_message_id, text, now_ms, sub.role, sub.id, project_id, handled_id),
        )
    else:
        handled_id = conn.execute(
            "INSERT INTO main.channel_handled (channel_id, project_id, message_id, role, role_key, sub_id, "
            "note, created_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (channel_id, project_id, up_to_message_id, sub.role, sub.role_key, sub.id, text, now_ms),
        ).lastrowid
    write_audit(
        conn,
        now_ms=now_ms,
        actor=principal.kind,
        api_token_id=principal.token_id,
        client_name=client_name,
        request_id=request_id,
        action="channel_handled",
        result="changed" if changed else "unchanged",
        channel_id=channel_id,
        detail={
            "handled_id": handled_id,
            "message_id": up_to_message_id,
            "sub_id": sub.id,
            "role": sub.role,
            "note_len": len(text),
            "closed_cards": closed,
        },
    )
    return {
        "id": handled_id,
        "channel_id": channel_id,
        "message_id": up_to_message_id,
        "role": sub.role,
        "note": text,
        "changed": changed,
        "closed_cards": closed,
    }


# --- реакция на сообщение = разобрано (Ф66-4) -------------------------------------------------------------


def reacted_up_to(conn, *, channel_id: int, message_id: int, now_ms: int) -> dict | None:
    """Ф66-4 A: реакция на чужое живое сообщение X значит «всё до X разобрано»: маркер прочтения до X
    (ответ в треде без `thread_broadcast` — маркер треда), карточки разборщика до X закрыты. Своё, служебное,
    удалённое, скрытое сообщение или без `ext_id` — `None`, ничего не пишется. Маркер только вперёд. Строку
    `channel_handled`, отметку в источник и аудит помощник не пишет — это дело вызывающего. Возвращает кусок
    `detail` аудита."""
    schema = db_of(channel_id)
    if db_of(message_id) != schema:
        return None
    message = conn.execute(
        "SELECT channel_id, ext_id, thread_ext_id, thread_broadcast, hidden, is_me, deleted, kind, ts, "  # noqa: S608
        f"ext_sort FROM {schema}.message WHERE id = ?",
        (message_id,),
    ).fetchone()
    if message is None or message[0] != channel_id:
        return None
    _c, ext_id, thread_ext_id, thread_broadcast, hidden, is_me, deleted, kind, msg_ts, msg_sort = message
    if hidden or is_me or deleted or ext_id is None or kind == "event":
        return None
    if threads_mod.is_reply(thread_ext_id, ext_id) and not thread_broadcast:
        try:
            changed = threads_mod.mark_thread_read(
                conn, schema, channel_id, thread_ext_id, up_to_message_id=message_id
            )
        except threads_mod.ThreadError:
            return None
        thread = True
    else:
        try:
            changed = channels_mod.mark_read(conn, channel_id, up_to_message_id=message_id, now_ms=now_ms)
        except channels_mod.InvalidReplyTarget:
            return None
        thread = False
    closed = agent_items.close_cards_up_to(
        conn, schema, channel_id, ts=msg_ts, ext_sort=msg_sort or 0, message_id=message_id
    )
    return {
        "reaction_read_up_to": message_id,
        "reaction_read": "changed" if changed else "unchanged",
        "reaction_thread": thread,
        "reaction_closed_cards": closed,
    }


# --- закрытие карточки сессией = «разобрано» (12.10.2026) ---------------------------------------------

CARD_NOTE = "карточку закрыла сессия без ответа"


def mark_closed_cards(
    conn,
    *,
    principal,
    session,
    channel_id: int,
    item_ids: list[int],
    now_ms: int,
    waiting=None,
    client_name: str | None = None,
    request_id: str = "",
) -> dict | None:
    """Сессия закрыла карточки беседы (`hub_triage_mark`) — та же отметка «разобрано» до последнего сообщения
    этих карточек, иначе строка «Нужна реакция» висит у владельца, хотя разбирать уже нечего (владелец 12.10.2026).
    Условия те же, что у `mark` (подписанная сессия проекта с ролью, включённый `session_handled`): не
    выполнены — ничего не пишется и возвращается `None`, закрытие карточек от этого не зависит."""
    if session is None or principal.is_ui or not item_ids:
        return None
    schema = db_of(channel_id)
    marks = ", ".join("?" for _ in item_ids)
    row = conn.execute(
        f"SELECT lm.id FROM {schema}.agent_item a "  # noqa: S608
        f"JOIN {schema}.message lm ON lm.id = a.last_message_id "
        f"WHERE a.channel_id = ? AND a.id IN ({marks}) "
        "ORDER BY lm.ts DESC, lm.ext_sort DESC, lm.id DESC LIMIT 1",
        (channel_id, *item_ids),
    ).fetchone()
    if row is None:
        return None
    project = conn.execute(
        f"SELECT project_id FROM {schema}.channel WHERE id = ?",  # noqa: S608
        (channel_id,),
    ).fetchone()
    name_row = conn.execute("SELECT name FROM main.api_token WHERE id = ?", (principal.token_id,)).fetchone()
    try:
        return mark(
            conn,
            principal=principal,
            token_name=name_row[0] if name_row and isinstance(name_row[0], str) else None,
            session=session,
            channel_id=channel_id,
            project_id=project[0] if project else None,
            up_to_message_id=row[0],
            note=CARD_NOTE,
            now_ms=now_ms,
            waiting=waiting,
            client_name=client_name,
            request_id=request_id,
        )
    except HandledError:
        return None
