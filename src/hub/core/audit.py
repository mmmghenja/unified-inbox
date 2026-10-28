"""Аудит пишущих HTTP-ручек `/api/v1`. Спецификация: docs/architecture.mdb.

Отдельно от `main.audit`-записей, которые пишет сам `outbox.py` (те — про переходы автомата и не знают о
HTTP-запросе). Здесь — одна строка на каждый пишущий HTTP-запрос, с `request_id` (= `X-Request-Id` ответа) и
`client_name` (= `X-Hub-Client` запроса, обрезанный до 64 символов вызывающей стороной). Текст сообщения сюда
не передаётся НИКОГДА — в `detail` допустима длина текста, не сам текст. Вызывать только внутри `Db.write()`,
в той же транзакции, что и само действие.
"""

import json


def write_audit(
    conn,
    *,
    now_ms: int,
    actor: str,
    api_token_id: int | None,
    client_name: str | None,
    request_id: str,
    action: str,
    result: str | None,
    channel_id: int | None = None,
    outbox_id: int | None = None,
    detail: dict | None = None,
) -> None:
    conn.execute(
        "INSERT INTO main.audit "
        "(ts, actor, api_token_id, client_name, request_id, action, result, channel_id, outbox_id, "
        "detail_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            now_ms,
            actor,
            api_token_id,
            client_name,
            request_id,
            action,
            result,
            channel_id,
            outbox_id,
            json.dumps(detail) if detail is not None else None,
        ),
    )
