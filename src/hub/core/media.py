"""Медиа: строки `media`, очередь загрузки, файлы на диске. Спецификация: docs/architecture.md–A3 и «Поправки после
plan-critic» П1–П15 — они главнее текста выше них).

Устройство — по образцу outbox: ядро коннектору не звонит; коннектор ждёт (`wait`), берёт задание лизом
(`lease`), качает у источника и заливает байты `PUT` (или шлёт `report`). Все функции, принимающие `conn`,
зовутся ВНУТРИ `Db.write()` (поток-писатель, открыта `BEGIN IMMEDIATE`), кроме явно помеченных «читатель».

Файлы: `<data_dir>/media/<account_id>/<sha[:2]>/<sha>` (каталоги 0700, файлы 0600), дедуп по sha256 в пределах
аккаунта. Проверка «файл уже есть», `rename` и `unlink` — в потоке-писателе, в одной транзакции с записью
строки (П5): файл удаляется только после подсчёта ссылок в той же транзакции.

Журнал — только `media_id` и класс ошибки: ни имён файлов, ни URL, ни текстов (правило логов плана).
"""

import json
import logging
import os
import shutil
import sqlite3
from pathlib import Path

from . import draft_channels
from .db import db_of, next_seq

_logger = logging.getLogger(__name__)

MEDIA_DIR_NAME = "media"
TMP_PREFIX = ".tmp"
TMP_CONN_DIR_NAME = ".tmp-conn"  # П4: временные файлы коннекторов, `<data_dir>/media/.tmp-conn/<account_id>/`

MIB = 1024 * 1024
GIB = 1024 * MIB
AUTO_MAX_BYTES = 20 * MIB  # A2.2/П6: автоматически и без `priority` — не больше 20 МиБ
CLICK_MAX_BYTES = 300 * MIB  # A2.3: по клику — не больше 300 МиБ, больше — `too_big`
MIN_FREE_BYTES = 5 * GIB  # П12: ниже — новые строки `deferred`, `PUT` -> 507

LEASE_MS = 10 * 60 * 1000
BACKOFF_MS = (60 * 1000, 5 * 60 * 1000, 30 * 60 * 1000)  # A2.4: 1 -> 5 -> 30 мин
MAX_ATTEMPTS = 5
MAX_LEASE_LIMIT = 10
# Ф31-1 решение 5: `failed` -> `pending` по запросу СЕССИИ (`hub_download`) — не чаще раза в 10 мин на медиа.
SESSION_REFETCH_GAP_MS = 10 * 60 * 1000
START_TS_META_KEY = "media_start_ts"

# A1: у этих типов есть скачиваемый файл; `location`/`contact`/`poll`/`other` строк не получают.
FETCHABLE_KINDS = frozenset({"photo", "video", "voice", "audio", "document", "sticker", "gif"})
# П12: фото/стикеры/голосовые — автоматически везде (в незаглушённых и неархивных беседах).
ALWAYS_AUTO_KINDS = frozenset({"photo", "sticker", "voice"})
# П14: строки `media` и `fetch` — только для этих источников. Ф11-3a A2: почта — всегда по клику.
# Ф40-1 D: Mattermost — как Slack.
MEDIA_SOURCES = frozenset({"telegram", "slack", "mail", "mattermost", "whatsapp"})
# Ф11-3a A1: вид вложения письма по его `mime` — картинка, которую браузер покажет, иначе документ.
MAIL_PHOTO_MIMES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})

STATUSES = ("deferred", "pending", "fetching", "ready", "failed", "gone", "too_big")
REPORT_OUTCOMES = frozenset({"gone", "temporary", "too_big", "auth"})
DETECTED = ("jpeg", "png", "webp", "gif", "mp4", "webm", "ogg", "mp3", "m4a", "pdf", "other")

_MAX_TEXT = 1000  # длина строковых полей вложения (имя, mime, src_id) — кривое длиннее обрезается
_MAX_ERROR = 200
_MAX_REF_BYTES = 8 * 1024

# Поля словаря вложения в выдаче (A3) — без `ref_json`, без служебного.
_OUT_COLUMNS = (
    "id",
    "message_id",
    "idx",
    "kind",
    "status",
    "mime",
    "size",
    "name",
    "width",
    "height",
    "duration_s",
    "detected",
)


class MediaError(Exception):
    pass


class NotFound(MediaError):
    pass


class Forbidden(MediaError):
    """Строка чужого аккаунта коннектора (A2.4: чужой -> 403)."""


class StaleLease(MediaError):
    pass


class RefetchPaused(MediaError):
    """Ф31-1 решение 5: сессия уже возвращала это `failed` в очередь меньше `SESSION_REFETCH_GAP_MS` назад —
    повтор не раньше `until_ms`."""

    def __init__(self, until_ms: int) -> None:
        super().__init__("refetch paused")
        self.until_ms = until_ms


class Conflict(MediaError):
    """Вложение нельзя скачать (не Telegram/Slack, не файл, одноразовое, текст стёрт) — 409."""


def _sql(schema: str, template: str, **parts: str) -> str:
    """Тот же приём, что `ingest._sql`: схема (`main`/`work`) и доверенные фрагменты, не ввод пользователя."""
    return template.format(schema=schema, **parts)


# --- файлы и каталоги ------------------------------------------------------------------------------------


def data_dir_of(conn: sqlite3.Connection) -> Path:
    """Каталог данных по соединению: файл `main` лежит прямо в нём (`Db.main_path = data_dir / ...`). Ingest
    получает только `conn`, а правилу ссылок (П5, П10) нужен путь к файлам медиа."""
    for _seq, name, file in conn.execute("PRAGMA database_list").fetchall():
        if name == "main" and file:
            return Path(file).parent
    raise RuntimeError("не найден файл main")


def media_root(data_dir: Path) -> Path:
    return data_dir / MEDIA_DIR_NAME


def account_dir(data_dir: Path, account_id: int) -> Path:
    return media_root(data_dir) / str(int(account_id))


def file_path(data_dir: Path, account_id: int, sha256: str) -> Path:
    return account_dir(data_dir, account_id) / sha256[:2] / sha256


def ensure_dir(path: Path) -> None:
    """Каталог (и недостающие родители в пределах него) — 0700 независимо от umask."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def free_bytes(data_dir: Path) -> int:
    """Свободно на разделе каталога данных (П12). Отдельной функцией — тесты подменяют её."""
    return shutil.disk_usage(data_dir).free


def disk_ok(data_dir: Path) -> bool:
    try:
        return free_bytes(data_dir) >= MIN_FREE_BYTES
    except OSError:
        return False


def cleanup_tmp(data_dir: Path) -> None:
    """Старт ядра (П5): убрать `media/**/.tmp*` (недописанные `PUT`) и содержимое `.tmp-conn` (коннектор,
    убитый посреди загрузки). По образцу `backup.cleanup_plaintext`: ссылки не разыменовываются."""
    root = media_root(data_dir)
    if root.is_symlink() or not root.is_dir():
        return
    stack = [root]
    while stack:
        current = stack.pop()
        for p in current.iterdir():
            if p.is_symlink():
                if p.name.startswith(TMP_PREFIX):
                    p.unlink(missing_ok=True)
                continue
            if p.is_dir():
                if p.name == TMP_CONN_DIR_NAME:
                    for child in p.iterdir():
                        if child.is_dir() and not child.is_symlink():
                            for sub in child.iterdir():
                                if sub.is_dir() and not sub.is_symlink():
                                    shutil.rmtree(sub, ignore_errors=True)
                                else:
                                    sub.unlink(missing_ok=True)
                        else:
                            child.unlink(missing_ok=True)
                elif p.name.startswith(TMP_PREFIX):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    stack.append(p)
            elif p.name.startswith(TMP_PREFIX):
                p.unlink(missing_ok=True)


def detect(head: bytes) -> str:
    """Тип по сигнатуре первых байт (A2.4). `mime` и имя из источника на тип не влияют НИКОГДА (A3): что не
    узнано — `other` (SVG, HTML, HEIC, TIFF, PSD, мусор) и отдаётся только скачиванием."""
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"OggS"):
        return "ogg"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"M4A ", b"M4B "):
            return "m4a"
        # HEIC/HEIF/AVIF — тоже ISO BMFF, но браузер их как видео не покажет: файл (П7).
        if brand in (
            b"heic",
            b"heix",
            b"hevc",
            b"hevx",
            b"heim",
            b"heis",
            b"mif1",
            b"msf1",
            b"avif",
            b"avis",
        ):
            return "other"
        return "mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm" if b"webm" in head[:64] else "other"
    if head.startswith(b"ID3"):
        return "mp3"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        version = (head[1] >> 3) & 0x3
        layer = (head[1] >> 1) & 0x3
        if version != 0x1 and layer == 0x1:  # MPEG audio layer III, версия не «reserved»
            return "mp3"
    return "other"


# --- разбор вложения события -----------------------------------------------------------------------------


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if value != value or value in (float("inf"), float("-inf")):  # NaN/inf
        return None
    number = int(value)
    # Ревью Ф8-5: целое больше int64 (JSON это допускает) уронило бы INSERT в `media` OverflowError и сделало
    # событие ядовитым (П11). Потолок 2^53 — с запасом для размеров, пикселей и секунд.
    return number if 0 <= number <= 2**53 else None


def _str_or_none(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str) or not value:
        return None
    return value[:_MAX_TEXT]


def strip_media_ref(attachments: list | dict | None) -> list | dict | None:
    """A2.1: `media_ref` — служебная ссылка коннектора, в `attachments_json` (БД и выдачу) не попадает."""
    if isinstance(attachments, dict):
        return {k: v for k, v in attachments.items() if k != "media_ref"}
    if isinstance(attachments, list):
        return [
            {k: v for k, v in item.items() if k != "media_ref"} if isinstance(item, dict) else item
            for item in attachments
        ]
    return attachments


def mail_attachment_kind(mime: object) -> str:
    """Ф11-3a A1: `photo` для `image/jpeg|png|webp|gif`, иначе `document`. Одно правило на коннектор почты
    (пишет `type` у вложения) и ядро (старые письма в БД без `type`)."""
    if isinstance(mime, str) and mime.split(";")[0].strip().lower() in MAIL_PHOTO_MIMES:
        return "photo"
    return "document"


def parse_attachment(item: object, source: str | None = None) -> dict | None:
    """Вложение события -> поля строки `media` или `None` (строки не будет). Кривые поля молча
    отбрасываются (П11): событие из-за них не становится ядовитым.

    Строки нет: тип без файла (A1); `ttl: true` — одноразовое фото/видео Telegram (П13); `external` —
    внешний файл Slack (`is_external`/`mode=external`/`hidden_by_limit`, П2). `gone: true` — файл удалён у
    источника (tombstone Slack, П10): новой строки не заводит, существующую переводит в `gone`.
    `source` — источник аккаунта: у почты вложение без `type` получает вид по `mime` (Ф11-3a A1)."""
    if not isinstance(item, dict):
        return None
    kind = item.get("type")
    if source == "mail" and "type" not in item:
        # Ф11-3a A1: письма, пришедшие до Ф11-3a, лежат в БД без `type` — вид выводится из `mime`.
        kind = mail_attachment_kind(item.get("mime"))
    if not isinstance(kind, str) or kind not in FETCHABLE_KINDS:
        return None
    if item.get("ttl") is True:
        return None
    if item.get("external") is True:
        return None
    ref = item.get("media_ref")
    ref_json = None
    if isinstance(ref, dict):
        try:
            encoded = json.dumps(ref, sort_keys=True, ensure_ascii=False)
        except TypeError, ValueError:
            encoded = None
        if encoded is not None and len(encoded.encode("utf-8")) <= _MAX_REF_BYTES:
            ref_json = encoded
    return {
        "kind": kind,
        "src_id": _str_or_none(item.get("src_id")),
        "mime": _str_or_none(item.get("mime")),
        "size": _int_or_none(item.get("size")),
        "name": _str_or_none(item.get("name")),
        "width": _int_or_none(item.get("width")),
        "height": _int_or_none(item.get("height")),
        "duration_s": _int_or_none(item.get("duration_s")),
        "ref_json": ref_json,
        "gone": item.get("gone") is True,
    }


# --- начальный статус и точка отсчёта ----------------------------------------------------------------------


def get_start_ts(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT value FROM main.meta WHERE key = ?", (START_TS_META_KEY,)).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        return int(row[0])
    except ValueError:
        return None


def ensure_start_ts(conn: sqlite3.Connection, now_ms: int) -> int:
    """`media_start_ts` — время первого старта ядра с этой миграцией (A1, П15: кодом, не SQL-миграцией).
    Ставится один раз; повторный старт её не двигает."""
    conn.execute(
        "INSERT INTO main.meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING",
        (START_TS_META_KEY, str(int(now_ms))),
    )
    value = get_start_ts(conn)
    assert value is not None  # noqa: S101 — только что вставили или уже было
    return value


def initial_status(
    *,
    kind: str,
    size: int | None,
    msg_ts: int,
    start_ts: int | None,
    source: str,
    channel_kind: str,
    muted: bool,
    archived: bool,
    disk_ok: bool,
) -> str:
    """A2.2 + П6 + П12: `pending` (качать сразу) или `deferred` (по клику)."""
    if source == "mail":
        return "deferred"  # Ф11-3a A2: вложения писем — только по клику (логотипы уведомлений не качаем)
    if start_ts is None or msg_ts < start_ts:
        return "deferred"  # стартовая загрузка и догон старше точки отсчёта
    if muted or archived or not disk_ok:
        return "deferred"
    if size is not None and size > AUTO_MAX_BYTES:
        return "deferred"
    if kind in ALWAYS_AUTO_KINDS:
        return "pending"
    if source == "telegram" and channel_kind == "channel":
        return "deferred"  # ленты каналов Telegram с видео — по клику (П12)
    if size is None:
        return "deferred"
    return "pending"


# --- seq и правило ссылок ----------------------------------------------------------------------------------


def bump_message_seq(conn: sqlite3.Connection, schema: str, message_id: int) -> int:
    """П8: любая смена статуса строки `media` — новый `seq` её сообщению (UI видит через `/sync`)."""
    seq = next_seq(conn)
    conn.execute(_sql(schema, "UPDATE {schema}.message SET seq = ? WHERE id = ?"), (seq, message_id))
    return seq


def release_file(conn: sqlite3.Connection, schema: str, account_id: int, sha256: str | None) -> None:
    """Правило ссылок (П5): после изменения строк в ЭТОЙ транзакции — если на `sha` в пределах аккаунта не
    осталось ни одной строки, файл удаляется. Вызывать ПОСЛЕ update/delete строки.

    Ф10-4 П2: ссылка — и неотправленная загрузка (`upload`) того же аккаунта с тем же sha256: владелец прикрепил
    файл, совпавший с уже скачанным, и удалил скачанное у себя — прикреплённое обязано уйти целым."""
    if not sha256:
        return
    refs = conn.execute(
        _sql(
            schema,
            "SELECT (SELECT COUNT(*) FROM {schema}.media WHERE account_id = ? AND sha256 = ?) "
            "+ (SELECT COUNT(*) FROM {schema}.upload WHERE account_id = ? AND sha256 = ?)",
        ),
        (account_id, sha256, account_id, sha256),
    ).fetchone()[0]
    if refs:
        return
    try:
        file_path(data_dir_of(conn), account_id, sha256).unlink(missing_ok=True)
    except OSError as exc:
        _logger.warning("media: файл не удалён (%s)", type(exc).__name__)


# --- синхронизация строк с сообщением (ingest) --------------------------------------------------------------

_ROW_COLUMNS = (
    "id",
    "idx",
    "kind",
    "src_id",
    "mime",
    "size",
    "name",
    "width",
    "height",
    "duration_s",
    "ref_json",
    "status",
    "sha256",
)
_META_FIELDS = ("mime", "size", "name", "width", "height", "duration_s")


def _account_source(conn: sqlite3.Connection, account_id: int) -> str | None:
    row = conn.execute("SELECT source FROM main.account WHERE id = ?", (account_id,)).fetchone()
    return row[0] if row is not None else None


def sync_for_message(
    conn: sqlite3.Connection,
    schema: str,
    *,
    account_id: int,
    channel_id: int,
    ext_id: str,
    attachments: list | dict | None,
    now_ms: int,
    create_only: bool = False,
    defer_new: bool = False,
) -> int:
    """Привести строки `media` сообщения `(channel_id, ext_id)` к вложениям события (A2.1, П10, П11). Зовётся
    на ВСЕХ ветках `ingest._apply_message` — вставка, правка, noop, эхо своей отправки. Возвращает новый `seq`
    сообщения, если что-то в его `media` поменялось (П8), иначе 0.

    `create_only` — событие устарело (его `edited_ts` старше уже применённой правки): только завести
    недостающие строки, существующие не трогать — старый снимок не должен откатить сменившийся файл.

    `defer_new` (Ф42-3) — событие добора истории: НОВЫЕ строки заводятся `deferred` (по клику), что бы ни
    решило `initial_status`; существующие строки — как обычно."""
    source = _account_source(conn, account_id)
    if source not in MEDIA_SOURCES:
        return 0
    row = conn.execute(
        _sql(
            schema,
            "SELECT m.id, m.ts, m.purged, c.kind, c.muted, c.archived FROM {schema}.message m "
            "JOIN {schema}.channel c ON c.id = m.channel_id WHERE m.channel_id = ? AND m.ext_id = ?",
        ),
        (channel_id, ext_id),
    ).fetchone()
    if row is None:
        return 0
    message_id, msg_ts, purged, channel_kind, muted, archived = row
    if purged:
        # П11: для `purged` строк не заводим. Уже заведённые не трогаем: содержимое события после очистки
        # сведено к `[{"type": …}]`, по нему не понять, осталось ли вложение, а повтор очищенного должен
        # оставаться noop без нового `seq` (ретенция, раздел F.1 спеки 2b).
        return 0

    desired: dict[int, dict] = {}
    if isinstance(attachments, list):
        for i, item in enumerate(attachments):
            spec = parse_attachment(item, source)
            if spec is not None:
                desired[i] = spec

    existing = {
        r[1]: dict(zip(_ROW_COLUMNS, r, strict=True))
        for r in conn.execute(
            _sql(
                schema, "SELECT {cols} FROM {schema}.media WHERE message_id = ?", cols=", ".join(_ROW_COLUMNS)
            ),
            (message_id,),
        ).fetchall()
    }
    if not desired and not existing:
        return 0

    start_ts = get_start_ts(conn)
    data_dir = data_dir_of(conn)
    disk_state: list[bool] = []

    def _disk_ok() -> bool:
        if not disk_state:
            disk_state.append(disk_ok(data_dir))
        return disk_state[0]

    def _initial(spec: dict) -> str:
        return initial_status(
            kind=spec["kind"],
            size=spec["size"],
            msg_ts=msg_ts,
            start_ts=start_ts,
            source=source,
            channel_kind=channel_kind,
            muted=bool(muted),
            archived=bool(archived),
            disk_ok=_disk_ok(),
        )

    changed = False
    for idx, spec in desired.items():
        current = existing.get(idx)
        if current is None:
            if spec["gone"]:
                continue  # tombstone без строки — качать нечего и помнить нечего
            conn.execute(
                _sql(
                    schema,
                    "INSERT INTO {schema}.media (message_id, account_id, idx, kind, src_id, mime, size, "
                    "name, width, height, duration_s, ref_json, status, created_ts) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ),
                (
                    message_id,
                    account_id,
                    idx,
                    spec["kind"],
                    spec["src_id"],
                    spec["mime"],
                    spec["size"],
                    spec["name"],
                    spec["width"],
                    spec["height"],
                    spec["duration_s"],
                    spec["ref_json"],
                    "deferred" if defer_new else _initial(spec),
                    now_ms,
                ),
            )
            changed = True
            continue
        if create_only:
            continue
        if _sync_existing(conn, schema, account_id, current, spec, _initial, multi=len(desired) >= 2):
            changed = True

    if not create_only:
        for idx, current in existing.items():
            if idx in desired:
                continue
            # Вложения больше нет (правка убрала файл, текст стёрт ретенцией) — строка уходит, файл — по
            # правилу ссылок (П10).
            conn.execute(_sql(schema, "DELETE FROM {schema}.media WHERE id = ?"), (current["id"],))
            release_file(conn, schema, account_id, current["sha256"])
            changed = True

    return bump_message_seq(conn, schema, message_id) if changed else 0


def _differs(spec: dict, current: dict, field: str) -> bool:
    return spec[field] is not None and current[field] is not None and spec[field] != current[field]


def _sync_existing(
    conn, schema: str, account_id: int, current: dict, spec: dict, initial, *, multi: bool = False
) -> bool:
    old_src = current["src_id"]
    new_src = spec["src_id"]
    # Ф11-3b П5: строка заготовки своей отправки (`ready`, `src_id IS NULL` — байты из загрузки владельца), а эхо
    # на том же `idx` несёт другое имя или размер (Slack переставил файлы сообщения) — байты заготовки уже не
    # этого вложения: строка сбрасывается на скачивание, как при смене `src_id`. Совпали — как раньше.
    # Только у сообщения с несколькими вложениями (`multi`): у одного вложения путать не с чем, а размер фото
    # Telegram в эхе всегда другой (пережато) — перекачка уже присланной картинки была бы впустую.
    placeholder_mismatch = (
        multi
        and current["status"] == "ready"
        and old_src is None
        and (_differs(spec, current, "name") or _differs(spec, current, "size"))
    )
    if (old_src is not None and new_src is not None and old_src != new_src) or placeholder_mismatch:
        # П10: медиа заменено правкой — строка в начальный статус, лиз загрузчика недействителен, старый
        # файл — по правилу ссылок.
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.media SET kind = ?, src_id = ?, mime = ?, size = ?, name = ?, width = ?, "
                "height = ?, duration_s = ?, ref_json = ?, status = ?, priority = 0, attempts = 0, "
                "lease_gen = lease_gen + 1, lease_expires_ts = NULL, not_before_ts = NULL, error = NULL, "
                "sha256 = NULL, stored_size = NULL, detected = NULL, fetched_ts = NULL WHERE id = ?",
            ),
            (
                spec["kind"],
                new_src,
                spec["mime"],
                spec["size"],
                spec["name"],
                spec["width"],
                spec["height"],
                spec["duration_s"],
                spec["ref_json"],
                initial(spec),
                current["id"],
            ),
        )
        release_file(conn, schema, account_id, current["sha256"])
        # Ф14-6 П4: строка та же, файл другой — расшифровка прежнего файла прочь; новая — при новом `ready`.
        from . import transcripts

        transcripts.drop(conn, schema, current["id"])
        return True

    if spec["gone"] and current["status"] not in ("ready", "gone"):
        # Tombstone Slack (П10). Уже скачанное остаётся видно (B6: удалённое отправителем — как было).
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.media SET status = 'gone', lease_gen = lease_gen + 1, "
                "lease_expires_ts = NULL, not_before_ts = NULL WHERE id = ?",
            ),
            (current["id"],),
        )
        return True

    updates: dict[str, object] = {}
    for field in _META_FIELDS:
        if spec[field] is not None and spec[field] != current[field]:
            updates[field] = spec[field]
    if old_src is None and new_src is not None:
        updates["src_id"] = new_src
    if spec["ref_json"] is not None and spec["ref_json"] != current["ref_json"]:
        updates["ref_json"] = spec["ref_json"]
    if not updates:
        return False
    assignments = ", ".join(f"{col} = ?" for col in updates)
    conn.execute(
        _sql(schema, "UPDATE {schema}.media SET {set_clause} WHERE id = ?", set_clause=assignments),
        (*updates.values(), current["id"]),
    )
    # Поля выдачи (имя, размер, пропорции) видит UI — бамп `seq`; служебные (`src_id`, `ref_json`) — нет.
    return any(field in updates for field in _META_FIELDS)


# --- выдача ------------------------------------------------------------------------------------------------


def media_by_message(conn: sqlite3.Connection, schema: str, message_ids: list[int]) -> dict[int, list[dict]]:
    """Читатель. `{message_id: [словарь вложения]}` ОДНИМ запросом на схему (A3): без `ref_json`, `src_id`,
    ошибки и лиза. Размер — от источника, а если его не было — фактический скачанного файла."""
    if not message_ids:
        return {}
    result: dict[int, list[dict]] = {}
    ids = sorted(set(message_ids))
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            _sql(
                schema,
                "SELECT id, message_id, idx, kind, status, mime, COALESCE(size, stored_size), name, width, "
                "height, duration_s, detected FROM {schema}.media WHERE message_id IN ({ph}) "
                "ORDER BY message_id, idx",
                ph=placeholders,
            ),
            chunk,
        ).fetchall()
        # Ф14-6 C: у элемента — `transcript: null | {status, text, lang, problem, manual}`.
        from . import transcripts

        by_media = transcripts.for_messages(conn, schema, chunk) if rows else {}
        for r in rows:
            d = dict(zip(_OUT_COLUMNS, r, strict=True))
            mid = d.pop("message_id")
            d["transcript"] = by_media.get(d["id"])
            result.setdefault(mid, []).append(d)
    return result


def attach_media(conn: sqlite3.Connection, schema: str, messages: list[dict]) -> None:
    """Мутирует `message["media"]` у сообщений одной схемы (A3: все выдачи сообщений). Ф14-6 П6: и
    `message["transcript_wait_ms"]` — сколько ещё ждать расшифровку (0 — не ждать), по нему разборщик держит
    беседу."""
    from . import transcripts

    ids = [m["id"] for m in messages]
    by_message = media_by_message(conn, schema, ids)
    waits = transcripts.wait_ms_for_messages(conn, schema, ids, now_ms=transcripts.now_ms()) if ids else {}
    for m in messages:
        m["media"] = by_message.get(m["id"], [])
        m["transcript_wait_ms"] = waits.get(m["id"], 0)


def message_media(conn: sqlite3.Connection, message_id: int) -> list[dict]:
    return media_by_message(conn, db_of(message_id), [message_id]).get(message_id, [])


# --- ручной «Загрузить» (A2.3, П6, П14) ---------------------------------------------------------------------


def request_fetch(conn: sqlite3.Connection, *, message_id: int, idx: int, now_ms: int) -> list[dict]:
    """`POST /messages/{id}/media/{idx}/fetch`: видимость сообщения уже проверена вызывающим. Нет строки
    (старое сообщение) — заводится сразу `pending` с `priority=1`; `deferred`/`failed` — в `pending` с
    `priority=1` и `attempts=0`. Размер известен и больше 300 МиБ — `too_big` сразу. Прочие статусы — как
    есть. Возвращает обновлённый `media` сообщения (П8)."""
    schema = db_of(message_id)
    row = conn.execute(
        _sql(
            schema,
            "SELECT account_id, attachments_json, purged FROM {schema}.message WHERE id = ?",
        ),
        (message_id,),
    ).fetchone()
    if row is None:
        raise NotFound("message")
    account_id, attachments_json, purged = row
    source = _account_source(conn, account_id)
    if source not in MEDIA_SOURCES or purged:
        raise Conflict("media not supported")
    try:
        attachments = json.loads(attachments_json) if attachments_json else None
    except ValueError:
        attachments = None
    if isinstance(attachments, dict):
        attachments = [attachments]
    if not isinstance(attachments, list) or not 0 <= idx < len(attachments):
        raise NotFound("attachment")
    spec = parse_attachment(attachments[idx], source)
    if spec is None:
        raise Conflict("attachment not fetchable")

    current = conn.execute(
        _sql(
            schema,
            "SELECT id, status, COALESCE(size, stored_size) FROM {schema}.media "
            "WHERE message_id = ? AND idx = ?",
        ),
        (message_id, idx),
    ).fetchone()
    if current is None:
        if spec["gone"]:
            raise Conflict("attachment gone")
        status = "too_big" if spec["size"] is not None and spec["size"] > CLICK_MAX_BYTES else "pending"
        conn.execute(
            _sql(
                schema,
                "INSERT INTO {schema}.media (message_id, account_id, idx, kind, src_id, mime, size, name, "
                "width, height, duration_s, ref_json, status, priority, created_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
            ),
            (
                message_id,
                account_id,
                idx,
                spec["kind"],
                spec["src_id"],
                spec["mime"],
                spec["size"],
                spec["name"],
                spec["width"],
                spec["height"],
                spec["duration_s"],
                spec["ref_json"],
                status,
                now_ms,
            ),
        )
        bump_message_seq(conn, schema, message_id)
    else:
        media_id, status, size = current
        if status in ("deferred", "failed"):
            new_status = "too_big" if size is not None and size > CLICK_MAX_BYTES else "pending"
            conn.execute(
                _sql(
                    schema,
                    "UPDATE {schema}.media SET status = ?, priority = 1, attempts = 0, not_before_ts = NULL, "
                    "lease_expires_ts = NULL, error = NULL WHERE id = ?",
                ),
                (new_status, media_id),
            )
            bump_message_seq(conn, schema, message_id)
    return media_by_message(conn, schema, [message_id]).get(message_id, [])


def request_fetch_for_session(conn: sqlite3.Connection, *, media_id: int, now_ms: int) -> bool:
    """Ф22-1 B2 + П3: запрос загрузки от сессии (`POST /media/{id}/fetch`, bearer); видимость строки уже
    проверена вызывающим. В отличие от клика владельца отсрочку источника сессия НЕ обходит: `deferred` ->
    `pending` с `priority=1` (как клик); `failed` -> `pending` с `priority=1` только если `not_before_ts` пуст
    или прошёл, и БЕЗ сброса `attempts`; иначе строка не меняется. Размер известен и больше 300 МиБ ->
    `too_big`. Прочие статусы — как есть. Возвращает, изменилась ли строка. Ф31-1 решение 5: `failed`, который
    сессия уже возвращала в очередь меньше 10 мин назад, — `RefetchPaused` (строка не меняется)."""
    schema = db_of(media_id)
    row = conn.execute(
        _sql(
            schema,
            "SELECT message_id, status, COALESCE(size, stored_size), not_before_ts FROM {schema}.media "
            "WHERE id = ?",
        ),
        (media_id,),
    ).fetchone()
    if row is None:
        raise NotFound("media")
    message_id, status, size, not_before_ts = row
    if status not in ("deferred", "failed"):
        return False
    if status == "failed" and not_before_ts is not None and not_before_ts > now_ms:
        return False
    if status == "failed":
        # Ф31-1 решение 5: повтор сессии по `failed` — не чаще раза в `SESSION_REFETCH_GAP_MS` (последний
        # успешный запрос сессии — аудит `media_fetch` `ok` с этим `media_id`; клик владельца пишет без него).
        last = conn.execute(
            "SELECT MAX(ts) FROM main.audit WHERE action = 'media_fetch' AND ts > ? AND result = 'ok' "
            "AND json_extract(detail_json, '$.media_id') = ?",
            (now_ms - SESSION_REFETCH_GAP_MS, media_id),
        ).fetchone()[0]
        if last is not None:
            raise RefetchPaused(last + SESSION_REFETCH_GAP_MS)
    if size is not None and size > CLICK_MAX_BYTES:
        _set_status(conn, schema, media_id, message_id, "too_big", priority=1)
        return True
    if status == "deferred":
        _set_status(
            conn,
            schema,
            media_id,
            message_id,
            "pending",
            priority=1,
            attempts=0,
            not_before_ts=None,
            error=None,
        )
    else:
        _set_status(conn, schema, media_id, message_id, "pending", priority=1, not_before_ts=None, error=None)
    return True


# --- очередь: wait / lease / report / PUT -------------------------------------------------------------


def _account_schema(conn: sqlite3.Connection, account_id: int) -> tuple[str, str] | None:
    row = conn.execute("SELECT db, status FROM main.account WHERE id = ?", (account_id,)).fetchone()
    return (row[0], row[1]) if row is not None else None


def has_leasable(conn: sqlite3.Connection, *, account_id: int, now_ms: int) -> bool:
    """Читатель. «Готово» для `wait`: `lease` выдал бы хоть что-то (как `outbox_queries.has_leasable`).
    Аккаунт `limited`/`auth_required` — нечего выдавать (П15)."""
    info = _account_schema(conn, account_id)
    if info is None or info[1] in ("limited", "auth_required"):
        return False
    schema = info[0]
    row = conn.execute(
        _sql(
            schema,
            "SELECT 1 FROM {schema}.media WHERE account_id = ? AND ("
            "(status = 'pending' AND (not_before_ts IS NULL OR not_before_ts <= ?)) "
            "OR (status = 'fetching' AND lease_expires_ts <= ?)) LIMIT 1",
        ),
        (account_id, now_ms, now_ms),
    ).fetchone()
    return row is not None


def _backoff_ms(attempts: int) -> int:
    return BACKOFF_MS[min(max(attempts, 1), len(BACKOFF_MS)) - 1]


def _expire_leases(conn: sqlite3.Connection, schema: str, account_id: int, now_ms: int) -> None:
    """Истёкший лиз -> `pending` (попытка засчитана, A2.4); пятая — `failed`. `lease_expires_ts` остаётся
    пометкой «не перехвачен» (П4)."""
    rows = conn.execute(
        _sql(
            schema,
            "SELECT id, message_id, attempts FROM {schema}.media "
            "WHERE account_id = ? AND status = 'fetching' AND lease_expires_ts <= ?",
        ),
        (account_id, now_ms),
    ).fetchall()
    for media_id, message_id, attempts in rows:
        attempts += 1
        status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.media SET status = ?, attempts = ?, error = 'lease expired' WHERE id = ?",
            ),
            (status, attempts, media_id),
        )
        bump_message_seq(conn, schema, message_id)
        _logger.info("media %s: лиз истёк", media_id)


def lease(conn: sqlite3.Connection, *, account_id: int, now_ms: int, limit: int = 1) -> list[dict]:
    """Задания своего аккаунта (П3): `priority DESC`, затем свежее сообщение первым (П15); лиз 10 мин."""
    info = _account_schema(conn, account_id)
    if info is None:
        return []
    schema, status = info
    _expire_leases(conn, schema, account_id, now_ms)
    if status in ("limited", "auth_required"):
        return []
    limit = max(1, min(int(limit), MAX_LEASE_LIMIT))
    rows = conn.execute(
        _sql(
            schema,
            "SELECT md.id, md.message_id, md.idx, md.kind, md.mime, md.size, md.name, md.src_id, "
            "md.ref_json, md.priority, c.ext_id, m.ext_id, m.thread_ext_id FROM {schema}.media md "
            "JOIN {schema}.message m ON m.id = md.message_id JOIN {schema}.channel c ON c.id = m.channel_id "
            "WHERE md.account_id = ? AND md.status = 'pending' "
            "AND (md.not_before_ts IS NULL OR md.not_before_ts <= ?) AND m.ext_id IS NOT NULL "
            "AND {not_temp} "  # Ф12-1 A7: беседа-заготовка коннектору не видна
            "ORDER BY md.priority DESC, m.ts DESC, md.id DESC LIMIT ?",
            not_temp=draft_channels.not_temp_sql("c"),
        ),
        (account_id, now_ms, limit),
    ).fetchall()
    jobs = []
    for (
        media_id,
        message_id,
        idx,
        kind,
        mime,
        size,
        name,
        src_id,
        ref_json,
        priority,
        channel_ext_id,
        ext_id,
        thread_ext_id,
    ) in rows:
        lease_gen = conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.media SET status = 'fetching', lease_gen = lease_gen + 1, "
                "lease_expires_ts = ? WHERE id = ? RETURNING lease_gen",
            ),
            (now_ms + LEASE_MS, media_id),
        ).fetchone()[0]
        bump_message_seq(conn, schema, message_id)
        try:
            ref = json.loads(ref_json) if ref_json else None
        except ValueError:
            ref = None
        jobs.append(
            {
                "media_id": media_id,
                "lease_gen": lease_gen,
                "max_bytes": CLICK_MAX_BYTES if priority else AUTO_MAX_BYTES,
                "message": {
                    "channel_ext_id": channel_ext_id,
                    "ext_id": ext_id,
                    "thread_ext_id": thread_ext_id,
                },
                "idx": idx,
                "kind": kind,
                "mime": mime,
                "size": size,
                "name": name,
                "src_id": src_id,
                "ref": ref,
            }
        )
    return jobs


_LEASE_ROW = (
    "SELECT account_id, message_id, status, lease_gen, lease_expires_ts, priority FROM {schema}.media "
    "WHERE id = ?"
)


def _leased_row(
    conn: sqlite3.Connection, media_id: int, account_id: int, lease_gen: int
) -> tuple[str, int, int]:
    """Строка под лизом этого коннектора: `(schema, message_id, priority)`. Чужая — `Forbidden`; лиз устарел —
    `StaleLease`. Лиз жив, пока `lease_gen` совпадает и строку не перехватили, даже если срок истёк (П4)."""
    schema = db_of(media_id)
    row = conn.execute(_sql(schema, _LEASE_ROW), (media_id,)).fetchone()
    if row is None:
        raise NotFound("media")
    row_account, message_id, status, row_gen, lease_expires_ts, priority = row
    if row_account != account_id:
        raise Forbidden("media")
    if row_gen != lease_gen or lease_expires_ts is None or status not in ("fetching", "pending", "failed"):
        raise StaleLease("media")
    return schema, message_id, priority


def check_lease(conn: sqlite3.Connection, *, media_id: int, account_id: int, lease_gen: int) -> int:
    """Читатель: проверка до приёма тела `PUT`. Возвращает потолок размера для этого задания."""
    _schema, _message_id, priority = _leased_row(conn, media_id, account_id, lease_gen)
    return CLICK_MAX_BYTES if priority else AUTO_MAX_BYTES


def _set_status(conn, schema: str, media_id: int, message_id: int, status: str, **extra: object) -> None:
    assignments = ["status = ?", "lease_expires_ts = NULL"]
    values: list[object] = [status]
    for key, value in extra.items():
        assignments.append(f"{key} = ?")
        values.append(value)
    conn.execute(
        _sql(
            schema, "UPDATE {schema}.media SET {set_clause} WHERE id = ?", set_clause=", ".join(assignments)
        ),
        (*values, media_id),
    )
    bump_message_seq(conn, schema, message_id)


def _temporary(
    conn,
    schema: str,
    media_id: int,
    message_id: int,
    now_ms: int,
    error: str | None,
    retry_after_s: int | None,
) -> None:
    status, current = conn.execute(
        _sql(schema, "SELECT status, attempts FROM {schema}.media WHERE id = ?"), (media_id,)
    ).fetchone()
    if status == "failed":
        # Ф32-2 п.5б + П5: поздний отчёт после истёкшего лиза, пятую попытку уже засчитал `_expire_leases` —
        # строка остаётся `failed`.
        return
    if retry_after_s is not None and retry_after_s >= 0:
        # П15: FloodWait — попыткой не считается, ждём ровно столько, сколько сказал источник.
        _set_status(
            conn,
            schema,
            media_id,
            message_id,
            "pending",
            not_before_ts=now_ms + retry_after_s * 1000,
            error=error,
        )
        return
    if status != "fetching":
        # Ф32-2 п.5б + П5: лиз истёк, попытку уже засчитал `_expire_leases` — поздний отчёт с тем же
        # `lease_gen` второй раз её не считает, только откладывает повтор по уже набранному числу попыток.
        _set_status(
            conn,
            schema,
            media_id,
            message_id,
            "pending",
            not_before_ts=now_ms + _backoff_ms(current),
            error=error,
        )
        return
    attempts = current + 1
    if attempts >= MAX_ATTEMPTS:
        _set_status(
            conn, schema, media_id, message_id, "failed", attempts=attempts, not_before_ts=None, error=error
        )
    else:
        _set_status(
            conn,
            schema,
            media_id,
            message_id,
            "pending",
            attempts=attempts,
            not_before_ts=now_ms + _backoff_ms(attempts),
            error=error,
        )


def report(
    conn: sqlite3.Connection,
    *,
    media_id: int,
    account_id: int,
    lease_gen: int,
    outcome: str,
    error: str | None,
    retry_after_s: int | None,
    now_ms: int,
    size: int | None = None,
) -> None:
    """A2.4: `gone` / `temporary` (бэкофф 1 -> 5 -> 30 мин, пятая попытка — `failed`; с `retry_after_s` —
    ровно столько ждать, попыткой не считается) / `too_big` / `auth` (`failed`, статус аккаунта не трогается —
    ошибку входа коннектор шлёт как обычно). `size` при `too_big` — фактический размер у источника (П6)."""
    if outcome not in REPORT_OUTCOMES:
        raise ValueError("unknown outcome")
    schema, message_id, priority = _leased_row(conn, media_id, account_id, lease_gen)
    error = error[:_MAX_ERROR] if isinstance(error, str) else None
    if outcome == "gone":
        _set_status(conn, schema, media_id, message_id, "gone", error=error)
    elif outcome == "temporary":
        _temporary(conn, schema, media_id, message_id, now_ms, error, retry_after_s)
    elif outcome == "too_big":
        # П6: размер известен — `deferred` с ним, если он в пределах 300 МиБ (дальше — по клику), иначе
        # `too_big`. Без размера: задание без `priority` — «больше 20 МиБ» (`deferred`), с ним — `too_big`.
        known = size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None
        if known is not None:
            status = "too_big" if known > CLICK_MAX_BYTES else "deferred"
            _set_status(conn, schema, media_id, message_id, status, error=error, size=known)
        else:
            _set_status(
                conn, schema, media_id, message_id, "too_big" if priority else "deferred", error=error
            )
    else:  # auth
        _set_status(conn, schema, media_id, message_id, "failed", error=error)


def reject_oversize(
    conn: sqlite3.Connection, *, media_id: int, account_id: int, lease_gen: int, size: int, now_ms: int
) -> None:
    """`PUT` длиннее потолка (A2.4, П6): без `priority` — `deferred` с известным размером, с ним —
    `too_big`."""
    del now_ms
    schema, message_id, priority = _leased_row(conn, media_id, account_id, lease_gen)
    _set_status(conn, schema, media_id, message_id, "too_big" if priority else "deferred", size=size)


def reject_disk_full(
    conn: sqlite3.Connection, *, media_id: int, account_id: int, lease_gen: int, now_ms: int
) -> None:
    """П12: свободно меньше 5 ГиБ — `PUT` -> 507, строка как после `temporary`."""
    schema, message_id, _priority = _leased_row(conn, media_id, account_id, lease_gen)
    _temporary(conn, schema, media_id, message_id, now_ms, "disk full", None)


def commit_upload(
    conn: sqlite3.Connection,
    *,
    media_id: int,
    account_id: int,
    lease_gen: int,
    tmp_path: Path,
    sha256: str,
    size: int,
    detected: str,
    now_ms: int,
) -> None:
    """Поток-писатель (П5): проверка лиза, дедуп, `rename` и запись `ready` — одной транзакцией. Устаревший
    лиз -> временный файл удаляется (ссылок на него нет по определению), `StaleLease`."""
    try:
        schema, message_id, _priority = _leased_row(conn, media_id, account_id, lease_gen)
    except MediaError:
        tmp_path.unlink(missing_ok=True)
        raise
    target = file_path(data_dir_of(conn), account_id, sha256)
    created = False
    if target.exists():
        tmp_path.unlink(missing_ok=True)  # дедуп: такой файл у аккаунта уже есть
    else:
        ensure_dir(target.parent)
        os.rename(tmp_path, target)
        created = True
    try:
        old_sha = conn.execute(
            _sql(schema, "SELECT sha256 FROM {schema}.media WHERE id = ?"), (media_id,)
        ).fetchone()[0]
        _set_status(
            conn,
            schema,
            media_id,
            message_id,
            "ready",
            sha256=sha256,
            stored_size=size,
            detected=detected,
            fetched_ts=now_ms,
            error=None,
            not_before_ts=None,
        )
        if old_sha and old_sha != sha256:
            release_file(conn, schema, account_id, old_sha)
        # Ф14-6 B: строка стала `ready` — кандидат автоматической расшифровки встаёт в очередь той же
        # транзакцией.
        from . import transcripts

        transcripts.on_media_ready(conn, media_id, now_ms=now_ms)
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise


# --- отдача ------------------------------------------------------------------------------------------------


def mark_missing(conn: sqlite3.Connection, *, media_id: int) -> None:
    """П5: `ready`, а файла нет (бэкап медиа не содержит, файл стёрт руками) -> `deferred`, «Загрузить»
    снова доступно."""
    schema = db_of(media_id)
    row = conn.execute(
        _sql(schema, "SELECT message_id, status FROM {schema}.media WHERE id = ?"), (media_id,)
    ).fetchone()
    if row is None or row[1] != "ready":
        return
    _set_status(
        conn,
        schema,
        media_id,
        row[0],
        "deferred",
        sha256=None,
        stored_size=None,
        detected=None,
        fetched_ts=None,
    )


# --- сводка (/status, hub-watch) ---------------------------------------------------------------------


def status_summary(conn: sqlite3.Connection, data_dir: Path) -> dict:
    """Читатель. Объём скачанного по аккаунтам (дедуп по sha — как на диске), `pending`/`failed` и свободное
    место (П12). Без имён."""
    by_account: dict[int, int] = {}
    pending = failed = 0
    for schema in ("main", "work"):
        for account_id, total in conn.execute(
            _sql(
                schema,
                "SELECT account_id, SUM(stored_size) FROM (SELECT DISTINCT account_id, sha256, stored_size "
                "FROM {schema}.media WHERE sha256 IS NOT NULL) GROUP BY account_id",
            )
        ).fetchall():
            by_account[account_id] = by_account.get(account_id, 0) + (total or 0)
        p, f = conn.execute(
            _sql(
                schema,
                "SELECT SUM(CASE WHEN status IN ('pending', 'fetching') THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) FROM {schema}.media",
            )
        ).fetchone()
        pending += p or 0
        failed += f or 0
    try:
        free = free_bytes(data_dir)
    except OSError:
        free = None
    return {
        "bytes_by_account": {str(k): v for k, v in sorted(by_account.items())},
        "total_bytes": sum(by_account.values()),
        "pending": pending,
        "failed": failed,
        "free_bytes": free,
    }


def summary_line(summary: dict) -> str:
    """«медиа: X ГБ, свободно Y ГБ» (П12) — одна строка для hub-watch и /status."""
    total = summary.get("total_bytes") or 0
    free = summary.get("free_bytes")
    free_text = "?" if free is None else f"{free / GIB:.1f}"
    return f"медиа: {total / GIB:.1f} ГБ, свободно {free_text} ГБ"
