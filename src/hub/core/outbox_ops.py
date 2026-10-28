"""Действия владельца над черновиком — одной функцией ядра на действие (Ф56-1а,
docs/architecture.md плана фазы): подтверждение и отмена. Зовут их маршрут
интерфейса (`POST /outbox/{id}/approve`, `/cancel`) и ответ из канала владельца (`owner_queue.answer`) — одно и
то же поведение, разница только в источнике.

Источник ответа (`source`): `ui` — клик владельца в интерфейсе; `agent` — ответ агента владельца из канала (токен со
скоупом `human`). Для автора и ленты это одинаково ответ владельца (П8); отличия агента (П9): правка текста не
пишется в `draft_text` (не входит в статистику правок владельца Ф19-1) и исход автору — `approved`, а не `edited`.
Статус Slack «На связи» (`touch_activity`) — дело маршрута, агентом не зажигается. `auto` — «сразу да» по
таблице маршрута проекта (Ф66-1, `card_route.auto_answer`): как клик владельца, но без отметки прочтения (П12).

Запись — только внутри `Db.write()`, время — параметром `now_ms`."""

from . import answered as answered_mod
from . import draft_outcomes as draft_outcomes_mod
from . import outbox as outbox_mod
from . import queries
from .audit import write_audit

SOURCES = ("ui", "agent", "auto")


def approve_detail(approved: outbox_mod.Approved, detail: dict) -> dict:
    """Аудит подтверждения: когда уйдёт и, у строки с `send_at`, — её время (Ф12-2 A3, Ф30-1 A3)."""
    if approved.mode is not None:
        detail["not_before_ts"] = approved.not_before_ts
    if approved.mode == "send_at":
        detail["send_at_ts"] = approved.send_at_ts
    return detail


def approve_draft(
    conn,
    outbox_id: int,
    *,
    principal,
    text: str | None,
    send_now: bool,
    now_ms: int,
    source: str,
    audit: dict,
) -> outbox_mod.Approved:
    """Подтвердить черновик (`pending_approval` -> `queued`) — то, что раньше делал маршрут
    `POST /outbox/{id}/approve` целиком: видимость строки принципалу (`queries.NotFound`), `approve_mode`
    (правка текста, график, `send_at`, «Отправить сейчас»), отметка прочтения по черновику
    (`answered.mark_on_approve`, Ф37-2), исход автору (`draft_outcomes.record`, Ф39 B5), аудит
    `outbox_approve`. `audit` — `{actor, api_token_id, client_name, request_id}`.

    Отказы — исключения `outbox`/`queries` как есть (`NotFound`, `SendAtPassed`, `InvalidTransition`,
    `OutboxError`): маршрут переводит их в коды."""
    if source not in SOURCES:
        raise ValueError(f"approve_draft: источник {source!r}")
    agent = source == "agent"
    channel_id = queries.outbox_visible(conn, principal, outbox_id)
    approved = outbox_mod.approve_mode(
        conn, outbox_id, text=text, now_ms=now_ms, send_now=send_now, record_edit=not agent
    )
    # Ф70-1 B.1: подтвердил сам владелец кукой интерфейса — строка уходит и пока аккаунт спит.
    outbox_mod.set_by_hand(conn, outbox_id, source == "ui")
    detail: dict = {}
    if text is not None:
        detail["text_len"] = len(text)
        # Ф19-1 B: правил ли владелец текст черновика (сам исходный текст — в `outbox.draft_text`).
        detail["edited"] = outbox_mod.draft_edited(conn, outbox_id)
    if send_now:
        detail["send_now"] = True
    approve_detail(approved, detail)
    # Ф37-2 A1: подтвердил черновик — значит прочитал то, на что он отвечает (в той же транзакции). Ф66-1
    # (П12): «сразу да» прочтение не двигает и отметку в источник не шлёт.
    if source != "auto":
        detail.update(answered_mod.mark_on_approve(conn, outbox_id, now_ms=now_ms) or {})
    # Ф39 B5: исход — автору черновика (с правкой текста владельца — `edited`; у агента правка не пишется).
    outcome = "edited" if outbox_mod.draft_edited(conn, outbox_id) else "approved"
    draft_outcomes_mod.record(conn, outbox_id, outcome, now_ms=now_ms)
    if source != "ui":
        detail["source"] = source
    write_audit(
        conn,
        now_ms=now_ms,
        action="outbox_approve",
        result="queued",
        channel_id=channel_id,
        outbox_id=outbox_id,
        detail=detail or None,
        **audit,
    )
    return approved


def cancel_draft(
    conn,
    outbox_id: int,
    *,
    principal,
    owner_token_id: int | None,
    record_outcome: bool,
    now_ms: int,
    audit: dict,
) -> str:
    """Отменить строку outbox — то, что делает маршрут `POST /outbox/{id}/cancel`: видимость
    (`queries.NotFound`), `outbox.cancel` (`owner_token_id` — отмена своего черновика сессией, спека 7a
    A4/П1), исход автору черновика при `record_outcome` (отмена владельцем или его агентом: `declined` из
    `pending_approval`, `withdrawn` из `queued`), аудит `outbox_cancel`. Возвращает новый статус."""
    channel_id = queries.outbox_visible(conn, principal, outbox_id)
    # Ф39 B5 + П6: прежний статус — в этой же транзакции ДО отмены (`outbox.cancel` отдаёт только новый).
    prev_status = outbox_mod.status_of(conn, outbox_id)
    new_status = outbox_mod.cancel(conn, outbox_id, now_ms=now_ms, owner_token_id=owner_token_id)
    if record_outcome and new_status == "cancelled":
        # Исход — автору черновика: владелец отклонил (`pending_approval`) или снял уже подтверждённый до
        # отправки (`queued`). `unknown` — могло уйти: без сигнала. Отмена самой сессией — без сигнала.
        outcome = "declined" if prev_status == "pending_approval" else "withdrawn"
        draft_outcomes_mod.record(conn, outbox_id, outcome, now_ms=now_ms)
    write_audit(
        conn,
        now_ms=now_ms,
        action="outbox_cancel",
        result=new_status,
        channel_id=channel_id,
        outbox_id=outbox_id,
        **audit,
    )
    return new_status
