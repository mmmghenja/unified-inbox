"""Ф99-2 (docs/architecture.md, поправки П5–П8): подписанная сессия глушит беседу и снимает СВОЮ
заглушку (`hub_mute`, `POST /channels/{id}/mute`).

- Доступ (П7): подписка сессии вызова с ролью на проект беседы (`autosend.role_sub`); беседа без проекта —
  подписка на проект, который слышит «Неразобранное» её аккаунта (Ф52-2, `unsorted.listening_projects`).
  Скоуп сам права не даёт. Беседа не в архиве и не заготовка `new:…`.
- Ручной режим владельца (`channel.attention` не `NULL`) — 409: заглушка сильнее ручного режима (`attention.py`),
  сессия его не перебивает (П6).
- `on=True` ставит `muted = 1` и `muted_by` (JSON: подписка, роль, причина, время). Беседа, уже заглушённая
  не сессией (`muted_by IS NULL`), не перехватывается — ответ «без изменений», снять её сессия не сможет.
- `on=False` снимает заглушку, только если её поставила сессия (`muted_by` не `NULL`); иначе 409 «заглушил
  владелец» (П5). Любой другой путь изменения `muted` сбрасывает `muted_by` (`channels.update_channel`, `join`,
  `ingest`).
- Предел — `MUTE_PER_HOUR` вызовов в час на подписку (429), считается по аудиту `channel_mute`.
- Аудит `channel_mute`: подписка, роль, длина причины (сама причина — в `muted_by`, как заметка стадии не
  идёт в аудит).

Уже открытые карточки и ждущие сигналы не закрываются (П8). Строки в ленте беседы ядро не пишет: служебных
строк ядра в ленте нет (`message` пишут коннекторы и outbox) — владельцу видна пометка беседы `muted_by_role` /
`muted_by_note` (`queries.get_channel`, `/sync`).

Все функции записи — только внутри `Db.write()`, время — параметром `now_ms`.
"""

import json

from . import autosend, draft_channels, unsorted
from .audit import write_audit
from .db import db_of, next_seq

NOTE_MIN = 3
NOTE_MAX = 200
MUTE_PER_HOUR = 100
_HOUR_MS = 60 * 60 * 1000


class MuteError(Exception):
    """Отказ: `status` — HTTP-код (403/404/409/422/429), `detail` — понятный текст без чужих данных."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def note_form(value: str | None) -> str:
    """Причина — одна строка 3..200 символов после обрезки пробелов."""
    note = " ".join((value or "").split())
    if len(note) < NOTE_MIN:
        raise MuteError(422, f"note: почему глушишь (или снимаешь) — от {NOTE_MIN} символов")
    if len(note) > NOTE_MAX:
        raise MuteError(422, f"note: не длиннее {NOTE_MAX} символов")
    return note


def parse_muted_by(raw: str | None) -> dict | None:
    """Значение столбца -> `{"sub", "role", "note", "ts"}`; `NULL` и кривое — `None` (не сессия)."""
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError, TypeError:
        return None
    return value if isinstance(value, dict) else None


def role_label(sub) -> str:
    """Роль подписки для людей: вторая линия роли — «роль 2»."""
    role = sub.role or "?"
    return role if (sub.role_no or 1) <= 1 else f"{role} {sub.role_no}"


def _find_sub(conn, *, principal, token_name, session, project_id, account_id, now_ms, waiting):
    """П7: подписка сессии с ролью на проект беседы; беседа без проекта — на любой проект, слышащий
    «Неразобранное» её аккаунта. Нет — `MuteError(403)` с причиной."""
    if project_id is not None:
        try:
            return autosend.role_sub(
                conn,
                principal=principal,
                token_name=token_name,
                session=session,
                project_id=project_id,
                now_ms=now_ms,
                waiting=waiting,
            )
        except autosend.NoRoleSub as exc:
            raise MuteError(403, f"заглушка беседы: {exc.reason}") from exc
    listening = [pid for pid, _muted in unsorted.listening_projects(conn, account_id)]
    if not listening:
        raise MuteError(
            403, "заглушка беседы: беседа без проекта, и ни один проект не слышит «Неразобранное» её аккаунта"
        )
    reason = None
    for pid in listening:
        try:
            return autosend.role_sub(
                conn,
                principal=principal,
                token_name=token_name,
                session=session,
                project_id=pid,
                now_ms=now_ms,
                waiting=waiting,
            )
        except autosend.NoRoleSub as exc:
            reason = exc.reason
    raise MuteError(
        403,
        "заглушка беседы без проекта: нужна живая подписка сессии с ролью на проект, который слышит "
        f"«Неразобранное» её аккаунта ({reason})",
    )


def _rate_count(conn, sub_id: int, now_ms: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM main.audit WHERE action = 'channel_mute' AND ts > ? "
        "AND result IN ('changed', 'unchanged') AND json_extract(detail_json, '$.sub_id') = ?",
        (now_ms - _HOUR_MS, sub_id),
    ).fetchone()
    return row[0]


def mute(
    conn,
    *,
    principal,
    token_name: str | None,
    session,
    channel_id: int,
    project_id: int | None,
    on: bool,
    note: str,
    now_ms: int,
    waiting=None,
    client_name: str | None = None,
    request_id: str = "",
) -> dict:
    """Заглушить (`on=True`) или снять свою заглушку (`on=False`). Видимость беседы токену и её проект
    (`project_id`) проверяет маршрут в той же транзакции (`queries.channel_visible`). Отказ ничего не
    пишет."""
    schema = db_of(channel_id)
    row = conn.execute(
        f"SELECT archived, ext_id, account_id, attention, muted, muted_by FROM {schema}.channel "  # noqa: S608
        "WHERE id = ?",
        (channel_id,),
    ).fetchone()
    if row is None:
        raise MuteError(404, "not found")
    archived, ext_id, account_id, attention, muted, muted_by_raw = row
    sub = _find_sub(
        conn,
        principal=principal,
        token_name=token_name,
        session=session,
        project_id=project_id,
        account_id=account_id,
        now_ms=now_ms,
        waiting=waiting,
    )
    text = note_form(note)
    if archived:
        raise MuteError(422, "беседа в архиве")
    if draft_channels.is_temp_ext_id(ext_id):
        raise MuteError(422, "новая беседа (заготовка) — глушить нечего")
    if attention is not None:
        raise MuteError(409, "режим беседы задал владелец — заглушку не ставлю и не снимаю")
    by = parse_muted_by(muted_by_raw)
    if not on and muted and by is None:
        raise MuteError(409, "заглушил владелец — снять может только он")
    if _rate_count(conn, sub.id, now_ms) >= MUTE_PER_HOUR:
        raise MuteError(429, f"лимит заглушек — {MUTE_PER_HOUR} в час на подписку — исчерпан")

    label = role_label(sub)
    before = {"muted": bool(muted), "muted_by_role": by.get("role") if by else None}
    changed = False
    if on and not muted:
        new_by = {"sub": sub.id, "role": label, "note": text, "ts": now_ms}
        conn.execute(
            f"UPDATE {schema}.channel SET muted = 1, muted_by = ?, seq = ? WHERE id = ?",  # noqa: S608
            (json.dumps(new_by, ensure_ascii=False), next_seq(conn), channel_id),
        )
        changed = True
    elif on and by is not None:
        # Уже заглушила сессия — обновляется только пометка (кто и почему), состояние то же.
        new_by = {"sub": sub.id, "role": label, "note": text, "ts": now_ms}
        conn.execute(
            f"UPDATE {schema}.channel SET muted_by = ?, seq = ? WHERE id = ?",  # noqa: S608
            (json.dumps(new_by, ensure_ascii=False), next_seq(conn), channel_id),
        )
    elif not on and muted:
        conn.execute(
            f"UPDATE {schema}.channel SET muted = 0, muted_by = NULL, seq = ? WHERE id = ?",  # noqa: S608
            (next_seq(conn), channel_id),
        )
        changed = True
    # Иначе: уже заглушена владельцем (`on=True`) или не заглушена (`on=False`) — без изменений.

    after_row = conn.execute(
        f"SELECT muted, muted_by FROM {schema}.channel WHERE id = ?",  # noqa: S608
        (channel_id,),
    ).fetchone()
    after_by = parse_muted_by(after_row[1])
    after = {"muted": bool(after_row[0]), "muted_by_role": after_by.get("role") if after_by else None}
    write_audit(
        conn,
        now_ms=now_ms,
        actor=principal.kind,
        api_token_id=principal.token_id,
        client_name=client_name,
        request_id=request_id,
        action="channel_mute",
        result="changed" if changed else "unchanged",
        channel_id=channel_id,
        detail={"on": bool(on), "sub_id": sub.id, "role": label, "note_len": len(text)},
    )
    return {
        "channel_id": channel_id,
        "on": bool(on),
        "changed": changed,
        "before": before,
        "after": after,
        "role": label,
        "note": text,
    }
