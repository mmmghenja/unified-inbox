"""«Подтвердил черновик — значит прочитал» (Ф37-2, docs/architecture.md).

Владелец подтвердил черновик (`POST /outbox/{id}/approve`, «Отправить сейчас», `approve-batch`) — беседа
отмечается прочитанной до последнего ЧУЖОГО сообщения верхнего уровня, которое было в беседе на момент
СОЗДАНИЯ черновика. Тем же кодом, что отметка из UI (`channels.mark_read` + `read_ack.enqueue`): отметка
уходит и в источник. Ответ в тред двигает только маркер этого треда. Авто-отправка Ф37-1 сюда не заходит (её
подтверждает не владелец, A4 и Ф37-1 П9); набранное владельцем в хабе — тоже (беседу он видит, отметку ставит UI).

Вызывается маршрутом подтверждения в той же транзакции, сразу после `outbox.approve_mode`.
"""

from . import channels as channels_mod
from . import draft_channels
from . import outbox as outbox_mod
from . import read_ack as read_ack_mod
from . import threads as threads_mod
from .db import db_of

# «Чужое сообщение»: не своё, не служебное, не удалённое, не скрытое у себя, дошедшее до источника.
_FOREIGN = "m.is_me = 0 AND m.kind <> 'event' AND m.deleted = 0 AND m.hidden = 0 AND m.ext_id IS NOT NULL"
_TOP_LEVEL = threads_mod.top_level_sql("m.thread_ext_id", "m.ext_id", "m.thread_broadcast")


def _last_foreign(conn, schema: str, channel_id: int, placeholder: tuple, where: str, params: tuple):
    """A1: id последнего по позиции чужого сообщения, которое было в беседе на момент создания черновика:
    вставлено раньше заготовки (`id` меньше) и стоит не позже неё по времени. Пришедшее после создания
    черновика сюда не попадает и остаётся непрочитанным."""
    placeholder_id, placeholder_ts = placeholder
    row = conn.execute(
        f"SELECT m.id FROM {schema}.message m WHERE m.channel_id = ? AND {_FOREIGN} AND {where} "  # noqa: S608
        "AND m.id < ? AND m.ts <= ? ORDER BY m.ts DESC, m.ext_sort DESC, m.id DESC LIMIT 1",
        (channel_id, *params, placeholder_id, placeholder_ts),
    ).fetchone()
    return row[0] if row is not None else None


def mark_on_approve(conn, outbox_id: int, *, now_ms: int) -> dict | None:
    """A1–A3: отметка прочтения по подтверждённому владельцем черновику. Возвращает кусок `detail` аудита
    подтверждения или `None`, если ничего не делалось:

    - обычный черновик — `{"read_up_to": id, "read": "changed"|"unchanged", "read_ack"?: …}`: маркер беседы
      до последнего чужого сообщения верхнего уровня не позже заготовки; сдвинулся — задание отметки в
      источник (`read_ack.enqueue`: `queued|off|unsupported`);
    - ответ в тред (A2) — `{"thread_read_up_to": id, "thread_read": …}`: только маркер треда, маркер беседы и
      отметка в источник не трогаются;
    - чужого сообщения до заготовки нет, беседа — заготовка `new:…` или `no_reply` (A3), строка — не черновик
      сессии или авто-отправка (A4) — `None`."""
    schema = db_of(outbox_id)
    row = conn.execute(
        f"SELECT o.channel_id, o.thread_ext_id, o.origin, o.auto_role, c.ext_id, c.meta_json "  # noqa: S608
        f"FROM {schema}.outbox o JOIN {schema}.channel c ON c.id = o.channel_id WHERE o.id = ?",
        (outbox_id,),
    ).fetchone()
    if row is None:
        return None
    channel_id, thread_ext_id, origin, auto_role, channel_ext, meta_json = row
    if origin != "api" or auto_role is not None:
        return None
    if draft_channels.is_temp_ext_id(channel_ext) or outbox_mod.channel_no_reply(meta_json):
        return None
    placeholder = conn.execute(
        f"SELECT id, ts FROM {schema}.message WHERE outbox_id = ? ORDER BY id LIMIT 1",  # noqa: S608
        (outbox_id,),
    ).fetchone()
    if placeholder is None:
        return None

    if thread_ext_id is not None:
        # A2: ответ в тред — последний чужой ответ ЭТОГО треда не позже заготовки.
        target = _last_foreign(
            conn,
            schema,
            channel_id,
            placeholder,
            "m.thread_ext_id = ? AND m.ext_id <> ?",
            (thread_ext_id, thread_ext_id),
        )
        if target is None:
            return None
        try:
            changed = threads_mod.mark_thread_read(
                conn, schema, channel_id, thread_ext_id, up_to_message_id=target
            )
        except threads_mod.ThreadError:
            return None  # строки треда нет — отмечать нечего
        return {"thread_read_up_to": target, "thread_read": "changed" if changed else "unchanged"}

    target = _last_foreign(conn, schema, channel_id, placeholder, _TOP_LEVEL, ())
    if target is None:
        return None
    changed = channels_mod.mark_read(conn, channel_id, up_to_message_id=target, now_ms=now_ms)
    detail: dict = {"read_up_to": target, "read": "changed" if changed else "unchanged"}
    if changed:
        # Как `POST /channels/{id}/read` (Ф8-7 A2): в источник толкается только то, что сдвинулось.
        detail["read_ack"] = read_ack_mod.enqueue(conn, channel_id, target, now_ms)
    return detail
