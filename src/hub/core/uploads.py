"""Загрузки для отправки файлов (Ф10-4, docs/architecture.md, A7 и поправки П1, П2, П5).

Загрузка привязана к беседе (П1): строка `upload` — в файле канала (`db_of(channel_id)`), байты — сразу
в складе медиа аккаунта канала (`media.file_path(data_dir, account_id, sha256)`, дедуп по sha256 в пределах
аккаунта). Файл склада удаляется только по правилу ссылок `media.release_file` — оно считает и строки
`media`, и строки `upload` того же аккаунта (П2).

Функции, принимающие `conn`, зовутся ВНУТРИ `Db.write()` (поток-писатель, открыта `BEGIN IMMEDIATE`), кроме
помеченных «читатель». Журнал — без имён файлов (П12).
"""

import os
import secrets
import sqlite3
import unicodedata
from pathlib import Path
from typing import NamedTuple

from . import media as media_mod
from .db import db_of

MIB = media_mod.MIB
MAX_UPLOAD_BYTES = 50 * MIB  # решение 5: больше — 413
# Ф11-3b решение 3 + П6: одно сообщение — до 10 файлов; сумма размеров — до 50 МиБ (Telegram, Slack, `fake`),
# у письма — до 17 МиБ (после base64 с переносами — меньше 25 МБ Gmail). Больше — 422 при постановке; файл
# письма больше 17 МиБ не принимает уже `POST /uploads` (413).
MAX_FILES_PER_MESSAGE = 10
MESSAGE_MAX_BYTES = 50 * MIB
MAIL_MAX_BYTES = 17 * MIB
PHOTO_MAX_BYTES = 10 * MIB  # A2: jpeg/png/webp не больше 10 МиБ — «фото», иначе документ
UPLOAD_TTL_MS = 24 * 60 * 60 * 1000  # решение 3, A7: неотправленные старше 24 ч удаляются
MAX_NAME = 200
# Решение 5 и C: файлы — в Telegram и Slack; `fake` — источник стенда (`scripts/dev-demo.py`), П12; почта —
# вложения в ответе на письмо (Ф11-3b A7; в новое письмо — нет, это проверяет `outbox.enqueue`).
# Ф40-1 D: Mattermost — до 10 файлов одним сообщением (не альбом), лимиты как у Slack.
SOURCES = frozenset({"telegram", "slack", "fake", "mail", "mattermost", "whatsapp"})
TELEGRAM_CAPTION_MAX_UTF16 = 1024  # П11: подпись к медиа Telegram
# Ф14-3 решение 4 + A2: квота загрузок bearer-токена (сессии Claude) — 30 файлов и 100 МиБ за скользящие 24 ч,
# по аудиту `upload` (без миграции). UI не ограничен.
SESSION_MAX_FILES = 30
SESSION_MAX_BYTES = 100 * MIB
SESSION_WINDOW_MS = 24 * 60 * 60 * 1000
# Ф14-4 A1: голосовое из браузера (`POST /uploads?voice=1`) — только запись webm/ogg, не больше 20 МиБ; вид
# `voice` хранится в `upload.kind` (миграция 0024), mime — ровно `audio/webm`/`audio/ogg`.
VOICE_MAX_BYTES = 20 * MIB
VOICE_KIND = "voice"
_VOICE_MIME = {"webm": "audio/webm", "ogg": "audio/ogg"}

_PHOTO_DETECTED = frozenset({"jpeg", "png", "webp"})
# Тип — только по сигнатуре байтов (`media.detect`); `Content-Type` клиента на него не влияет (A2).
_MIME_BY_DETECTED = {
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
    "mp4": "video/mp4",
    "webm": "video/webm",
    "ogg": "audio/ogg",
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "pdf": "application/pdf",
}


def _sql(schema: str, template: str) -> str:
    """Тот же приём, что `media._sql`: схема (`main`/`work`) — не ввод пользователя."""
    return template.format(schema=schema)


class UploadError(Exception):
    pass


class NotFound(UploadError):
    """Канала нет (или он исчез между проверкой и записью)."""


class QuotaExceeded(UploadError):
    """Ф14-3 A2: квота загрузок токена за сутки исчерпана (429, текст — с остатком)."""


def token_usage(conn: sqlite3.Connection, *, api_token_id: int, now_ms: int) -> tuple[int, int]:
    """Читатель или писатель: `(число, сумма байт)` успешных загрузок токена за последние 24 ч (аудит
    `upload`, индекс `audit_action_ts`; пересылка `from-media` пишет тот же аудит и входит в квоту)."""
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(CAST(json_extract(detail_json, '$.size') AS INTEGER)), 0) "
        "FROM main.audit WHERE action = 'upload' AND ts > ? AND result = 'ok' AND api_token_id = ?",
        (now_ms - SESSION_WINDOW_MS, api_token_id),
    ).fetchone()
    return int(row[0]), int(row[1])


def check_quota(conn: sqlite3.Connection, *, api_token_id: int, size: int, now_ms: int) -> None:
    """Ф14-3 A2 + П4: ещё один файл `size` байт не выводит токен за 30 файлов / 100 МиБ за сутки, иначе
    `QuotaExceeded` с остатком. Зовётся дважды: читателем до приёма тела (быстрый отказ) и писателем перед
    `commit` (окончательно — параллельные загрузки)."""
    count, total = token_usage(conn, api_token_id=api_token_id, now_ms=now_ms)
    if count + 1 > SESSION_MAX_FILES or total + size > SESSION_MAX_BYTES:
        left_files = max(SESSION_MAX_FILES - count, 0)
        left_mib = max(SESSION_MAX_BYTES - total, 0) / MIB
        raise QuotaExceeded(
            f"квота файлов сессии: {SESSION_MAX_FILES} файлов и {SESSION_MAX_BYTES // MIB} МиБ за сутки; "
            f"осталось {left_files} файлов и {left_mib:.1f} МиБ"
        )


def new_id() -> str:
    """128 случайных бит, base64url без `=` (22 символа)."""
    return secrets.token_urlsafe(16)


def mime_of(detected: str, kind: str | None = None) -> str:
    """Ф14-4 A1: у голосового (`kind='voice'`) webm — `audio/webm`, не видео."""
    if kind == VOICE_KIND and detected in _VOICE_MIME:
        return _VOICE_MIME[detected]
    return _MIME_BY_DETECTED.get(detected, "application/octet-stream")


def voice_ok(detected: str) -> bool:
    """Ф14-4 A1: голосовое — только запись webm или ogg."""
    return detected in _VOICE_MIME


def voice_name(detected: str) -> str:
    """Ф14-4 A1: имя голосового, если `X-Filename` пуст."""
    return f"voice.{detected}"


def kind_of(detected: str, size: int) -> str:
    """A2: `photo` — jpeg/png/webp не больше 10 МиБ, иначе `document`."""
    return "photo" if detected in _PHOTO_DETECTED and size <= PHOTO_MAX_BYTES else "document"


def clean_name(raw: str | None) -> str:
    """Решение 5: имя из заголовка — без пути, управляющих и невидимых символов (Cc, Cf — подмена направления
    текста, Cs, Zl, Zp), не длиннее 200 символов, не пустое."""
    value = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    value = "".join(ch for ch in value if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Zl", "Zp"))
    value = value.strip().strip(".").strip()
    if not value:
        value = "file"
    return value[:MAX_NAME]


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def commit(
    conn: sqlite3.Connection,
    *,
    upload_id: str,
    channel_id: int,
    account_id: int,
    tmp_path: Path,
    sha256: str,
    size: int,
    detected: str,
    name: str,
    now_ms: int,
    kind: str | None = None,
) -> None:
    """Поток-писатель (П1, как `media.commit_upload`): канал ещё есть, дедуп, `rename` и строка `upload` —
    одной транзакцией. Файл, созданный здесь, при сбое записи строки удаляется (ссылок на него нет)."""
    schema = db_of(channel_id)
    row = conn.execute(
        _sql(schema, "SELECT account_id FROM {schema}.channel WHERE id = ?"),
        (channel_id,),
    ).fetchone()
    if row is None or row[0] != account_id:
        tmp_path.unlink(missing_ok=True)
        raise NotFound("channel")
    target = media_mod.file_path(media_mod.data_dir_of(conn), account_id, sha256)
    created = False
    if target.exists():
        tmp_path.unlink(missing_ok=True)  # дедуп: такой файл у аккаунта уже есть
    else:
        media_mod.ensure_dir(target.parent)
        os.rename(tmp_path, target)
        created = True
    try:
        conn.execute(
            _sql(
                schema,
                "INSERT INTO {schema}.upload (id, channel_id, account_id, sha256, size, mime, detected, "
                "name, created_ts, kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ),
            (upload_id, channel_id, account_id, sha256, size, mime_of(detected, kind), detected, name, now_ms,
             kind),  # fmt: skip
        )
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise


def commit_existing(
    conn: sqlite3.Connection,
    *,
    upload_id: str,
    channel_id: int,
    account_id: int,
    sha256: str,
    size: int,
    detected: str,
    name: str,
    now_ms: int,
) -> None:
    """Поток-писатель (Ф14-3 A3): строка `upload` на файл, УЖЕ лежащий на складе аккаунта (пересылка вложения
    той же учётки — без копии). Канала нет или файла на складе нет — `NotFound`."""
    schema = db_of(channel_id)
    row = conn.execute(
        _sql(schema, "SELECT account_id FROM {schema}.channel WHERE id = ?"),
        (channel_id,),
    ).fetchone()
    if row is None or row[0] != account_id:
        raise NotFound("channel")
    if not media_mod.file_path(media_mod.data_dir_of(conn), account_id, sha256).is_file():
        raise NotFound("file")
    conn.execute(
        _sql(
            schema,
            "INSERT INTO {schema}.upload (id, channel_id, account_id, sha256, size, mime, detected, "
            "name, created_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ),
        (upload_id, channel_id, account_id, sha256, size, mime_of(detected), detected, name, now_ms),
    )


def get(conn: sqlite3.Connection, *, upload_id: str, channel_id: int) -> dict | None:
    """Загрузка ЭТОЙ беседы (П1: загрузка другого канала — как неизвестная) или `None`."""
    schema = db_of(channel_id)
    row = conn.execute(
        _sql(
            schema,
            "SELECT account_id, sha256, size, mime, detected, name, kind FROM {schema}.upload "
            "WHERE id = ? AND channel_id = ?",
        ),
        (upload_id, channel_id),
    ).fetchone()
    if row is None:
        return None
    account_id, sha256, size, mime, detected, name, kind = row
    return {
        "account_id": account_id,
        "sha256": sha256,
        "size": size,
        "mime": mime,
        "detected": detected,
        "name": name,
        # Ф14-4 (миграция 0024): вид строки, если задан (`voice`), иначе — прежнее вычисление.
        "kind": kind or kind_of(detected, size),
    }


def delete(conn: sqlite3.Connection, *, upload_id: str, channel_id: int) -> None:
    """Постановка в очередь (П1): строка уходит, ссылкой на файл становится строка `media` заготовки —
    поэтому файл здесь НЕ освобождается."""
    schema = db_of(channel_id)
    conn.execute(_sql(schema, "DELETE FROM {schema}.upload WHERE id = ?"), (upload_id,))


def cleanup(conn: sqlite3.Connection, *, now_ms: int) -> int:
    """A7 + П2: неотправленные загрузки старше 24 ч — строка удаляется, файл — только если на его sha256 в
    пределах аккаунта не осталось ни строк `media`, ни строк `upload` (одной транзакцией). Возвращает число
    удалённых строк."""
    cutoff = now_ms - UPLOAD_TTL_MS
    removed = 0
    for schema in ("main", "work"):
        rows = conn.execute(
            _sql(schema, "SELECT id, account_id, sha256 FROM {schema}.upload WHERE created_ts < ?"),
            (cutoff,),
        ).fetchall()
        for upload_id, account_id, sha256 in rows:
            conn.execute(_sql(schema, "DELETE FROM {schema}.upload WHERE id = ?"), (upload_id,))
            media_mod.release_file(conn, schema, account_id, sha256)
            removed += 1
    return removed


def channel_target(conn: sqlite3.Connection, channel_id: int) -> tuple[int, str | None] | None:
    """Читатель: `(account_id, source)` канала или `None`. Видимость принципалу проверяет маршрут."""
    schema = db_of(channel_id)
    row = conn.execute(
        _sql(
            schema,
            "SELECT c.account_id, a.source FROM {schema}.channel c "
            "LEFT JOIN main.account a ON a.id = c.account_id WHERE c.id = ?",
        ),
        (channel_id,),
    ).fetchone()
    return (row[0], row[1]) if row is not None else None


def channel_meta(conn: sqlite3.Connection, channel_id: int) -> str | None:
    """Читатель: `meta_json` канала (Ф14-3 A2: беседа трекера `no_reply` файлов не принимает)."""
    schema = db_of(channel_id)
    row = conn.execute(
        _sql(schema, "SELECT meta_json FROM {schema}.channel WHERE id = ?"), (channel_id,)
    ).fetchone()
    return row[0] if row is not None else None


class ForwardSource(NamedTuple):
    """Ф14-3 A3: вложение хаба для пересылки (`media:<id>`)."""

    account_id: int
    sha256: str
    size: int
    detected: str | None
    name: str | None


class NotReady(UploadError):
    """Ф14-3 A3: вложение видно, но ещё не скачано (или файла нет на складе) — 422."""


def forward_source(conn: sqlite3.Connection, principal, media_id: int) -> ForwardSource:
    """Читатель или писатель (Ф14-3 A3 + П5): вложение `media_id`, видимое принципалу. Видимость — как у
    `GET /media/{id}` (`queries.media_file`: скоуп, готовность, скрытое — одинаковый `queries.NotFound`) плюс
    сообщение не удалено у источника (`deleted = 0`, иначе тот же `NotFound`). Видимое, но не готовое или без
    файла на складе — `NotReady`."""
    from . import queries  # queries импортирует модули ядра; здесь — лениво, без цикла

    try:
        found = queries.media_file(conn, principal, media_id)
    except queries.NotFound:
        schema = db_of(media_id)
        row = conn.execute(
            _sql(schema, "SELECT message_id, status FROM {schema}.media WHERE id = ?"), (media_id,)
        ).fetchone()
        if row is None:
            raise
        queries.message_visible(conn, principal, row[0])  # чужое/скрытое — NotFound, как выше
        if row[1] == "ready":
            raise
        raise NotReady("вложение ещё не скачано") from None
    schema = db_of(media_id)
    row = conn.execute(
        _sql(
            schema,
            "SELECT md.stored_size, md.size, m.deleted FROM {schema}.media md "
            "JOIN {schema}.message m ON m.id = md.message_id WHERE md.id = ?",
        ),
        (media_id,),
    ).fetchone()
    if row is None or row[2]:
        raise queries.NotFound(f"медиа {media_id} не найдено")
    path = media_mod.file_path(media_mod.data_dir_of(conn), found.account_id, found.sha256)
    try:
        size = path.stat().st_size
    except OSError:
        raise NotReady("вложение ещё не скачано") from None
    return ForwardSource(found.account_id, found.sha256, size, found.detected, found.name)
