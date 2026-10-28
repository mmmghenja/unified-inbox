"""Читающие запросы к outbox для внутреннего сокета — отдельно от `outbox.py`, который не трогаем без
необходимости (принят в куске 3). Спецификация: docs/architecture.md, раздел `/outbox/wait`.

`has_leasable` повторяет условия выдачи из `outbox.lease` (FIFO по каналу, `not_before_ts`, `flood_until`,
стоп новых людей Ф69-1, сон аккаунта Ф70-1, истёкший лиз), но НИЧЕГО не пишет и лиз не берёт — только смотрит,
выдал бы `lease()` хоть одну строку.
Числовые константы (`_MAX_ATTEMPTS`) и признак идемпотентности берём из `outbox.py`, а не дублируем —
чтобы условие «готовности» не расходилось с условием реальной выдачи при будущих правках порога попыток.
"""

from . import peer_flood as peer_flood_mod
from .db import db_of
from .outbox import (  # noqa: PLC2701 — намеренное переиспользование, см. выше
    _MAX_ATTEMPTS,
    SleptLate,
    _is_idempotent,
    _parse_file_json,
    account_sleeping,
    fifo_params,
    fifo_sql,
    sleep_held,
)


def _sql(schema: str, template: str, **parts: str) -> str:
    return template.format(schema=schema, **parts)


def has_leasable(conn, *, account_id: int, now_ms: int) -> bool:
    account_row = conn.execute(
        "SELECT db, flood_until, config_json, peer_flood_ts FROM main.account WHERE id = ?", (account_id,)
    ).fetchone()
    if account_row is None:
        return False
    schema, flood_until, config_json, peer_flood_ts = account_row
    idempotent = _is_idempotent(config_json)
    held_new_peer = peer_flood_mod.held_checker(conn, schema, peer_flood_ts)  # Ф69-1 П1: как в `lease`
    sleeping = account_sleeping(conn, account_id, now_ms)  # Ф70-1 B.1: как в `lease`
    late = SleptLate(conn, account_id)  # Ф70-1: как в `lease`

    if flood_until is None or flood_until <= now_ms:
        fresh = conn.execute(
            _sql(
                schema,
                "SELECT id, channel_id, send_at_ts, by_hand, paced FROM {schema}.outbox WHERE account_id = ? "
                "AND status = 'queued' AND not_before_ts <= ? ORDER BY id",
            ),
            (account_id, now_ms),
        ).fetchall()
        for outbox_id, channel_id, send_at_ts, by_hand, paced in fresh:
            if sleeping and sleep_held(by_hand, paced, send_at_ts):
                continue  # Ф70-1 B.1: ждёт начала дня — `lease` её не выдаст и не тронет
            if late(send_at_ts, now_ms):
                return True  # Ф30-1 решение 3: `lease` закроет опоздавшую строку и без FIFO
            if held_new_peer(channel_id):
                continue  # Ф69-1 П1: стоп новых людей — `lease` эту строку не выдаст и не тронет
            # Ф12-2: строка графика с просроченным слотом — тоже «есть работа»: `lease` её выдаст, переставит
            # в новый слот или закроет устаревшее касание (П1, П2); запланированная впереди беседу не держит.
            blocked = conn.execute(
                _sql(schema, fifo_sql(sleeping)), fifo_params(channel_id, now_ms, outbox_id)
            ).fetchone()
            if blocked is None:
                return True

    stale = conn.execute(
        _sql(
            schema,
            "SELECT attempts FROM {schema}.outbox WHERE account_id = ? AND status = 'sending' "
            "AND lease_expires_ts < ?",
        ),
        (account_id, now_ms),
    ).fetchall()
    for (attempts,) in stale:
        if attempts > _MAX_ATTEMPTS:
            # lease() такую строку не выдаст, но именно он переводит её в unknown. Ответить здесь «нечего
            # выдавать» = коннектор никогда не позовёт lease(), и строка молча застрянет в sending навсегда.
            return True
        if idempotent and flood_until is not None and flood_until > now_ms:
            continue
        return True

    return False


def outbox_account_id(conn, outbox_id: int) -> int | None:
    """`account_id` строки outbox, если она существует, иначе `None` — используется, чтобы отличить «чужой
    outbox_id» (другой account_id) от несуществующего ДО вызова `outbox.report`, который сам этого не умеет
    (принимает только owner/lease_gen, не account_id)."""
    schema = db_of(outbox_id)
    row = conn.execute(
        _sql(schema, "SELECT account_id FROM {schema}.outbox WHERE id = ?"), (outbox_id,)
    ).fetchone()
    return row[0] if row is not None else None


def file_for_lease(
    conn, *, outbox_id: int, account_id: int, owner: str, lease_gen: int, now_ms: int, idx: int = 0
) -> tuple[int, str] | None:
    """Ф10-4 A5 + П9, Ф11-3b A4: `(account_id, sha256)` файла `idx` строки (по порядку вложений), если её
    держит ЭТОТ коннектор живым лизом того же поколения (`sending`, свой аккаунт и владелец, `lease_gen`
    совпал, срок не истёк); иначе `None` — ручка отвечает 404, не различая «чужое», «устарело», «нет файла» и
    «`idx` вне списка». Разбор `file_json` — общий `outbox._parse_file_json` (старая строка — словарь)."""
    schema = db_of(outbox_id)
    row = conn.execute(
        _sql(
            schema,
            "SELECT account_id, status, lease_owner, lease_gen, lease_expires_ts, file_json "
            "FROM {schema}.outbox WHERE id = ?",
        ),
        (outbox_id,),
    ).fetchone()
    if row is None:
        return None
    row_account, status, lease_owner, row_gen, lease_expires_ts, file_json = row
    if row_account != account_id or status != "sending" or lease_owner != owner or row_gen != lease_gen:
        return None
    if lease_expires_ts is None or lease_expires_ts < now_ms:
        return None
    files = _parse_file_json(file_json) or []
    if not 0 <= idx < len(files):
        return None
    sha256 = files[idx].get("sha256")
    if not isinstance(sha256, str) or not sha256:
        return None
    return row_account, sha256
