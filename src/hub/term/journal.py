"""Журнал запусков `main.term_run` (раздел 2.3) и счётчик номеров терминалов в `main.meta` (К7).

Функции, пишущие в базу, вызываются только внутри `Db.write()`. Свободный ввод (`input`) сюда не пишется
никогда — в нём пароли."""

RETENTION_MS = 30 * 24 * 60 * 60 * 1000
SEQ_KEY = "term_seq"
REF_MAX = 200
LIMIT_MAX = 200


def next_term_id(conn) -> int:
    """Новый номер терминала: растёт и не переиспользуется (переживает перезапуск ядра и ноутбука)."""
    row = conn.execute(
        "INSERT INTO main.meta (key, value) VALUES (?, '1') "
        "ON CONFLICT (key) DO UPDATE SET value = CAST(value AS INTEGER) + 1 RETURNING value",
        (SEQ_KEY,),
    ).fetchone()
    return int(row[0])


def record_run(
    conn, *, now_ms: int, term_id: int, who: str, source_kind: str, source_ref: str | None, text: str
) -> int:
    cur = conn.execute(
        "INSERT INTO main.term_run (ts, term_id, who, source_kind, source_ref, text) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (now_ms, term_id, who, source_kind, source_ref, text),
    )
    return cur.lastrowid


def list_runs(conn, *, limit: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, ts, term_id, who, source_kind, source_ref, text FROM main.term_run "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    keys = ("id", "ts", "term_id", "who", "source_kind", "source_ref", "text")
    return [dict(zip(keys, row, strict=True)) for row in rows]


def cleanup(conn, *, now_ms: int) -> int:
    """Срок хранения — 30 дней (ежечасная чистка ядра)."""
    return conn.execute("DELETE FROM main.term_run WHERE ts < ?", (now_ms - RETENTION_MS,)).rowcount
