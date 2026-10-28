"""Автомат исходящих (outbox). Спецификация: docs/architecture.md.

Все функции вызываются только внутри `Db.write()` (там уже открыта `BEGIN IMMEDIATE`, conn — писатель с обоими
файлами, main и work, под алиасами). Время — всегда параметром `now_ms`, никакого `time.time()` здесь.

Строки `outbox` и её заготовка в `message` лежат в файле канала (`schema = db_of(channel_id)`;
`db_of(outbox_id)` даёт то же, поскольку id из одного диапазона). `main.audit` получает только идентификаторы
и статусы — текст сообщения в audit никогда не попадает.
"""

import hashlib
import json
import random
import uuid
from typing import NamedTuple

from . import conn_sleep, draft_channels, extsort, pace, retention
from . import media as media_mod
from . import peer_flood as peer_flood_mod
from . import threads as threads_mod
from . import transcripts as transcripts_mod
from . import uploads as uploads_mod
from .db import db_of, next_seq

_DAY_MS = 24 * 60 * 60 * 1000
_UNTRUSTED_CHANNEL_AGE_MS = 14 * _DAY_MS
_RECENT_RENAME_MS = 7 * _DAY_MS
_ECHO_WINDOW_MS = 60_000
_MAX_ATTEMPTS = 5
_BACKOFF_BASE_MS = 5_000
_BACKOFF_CAP_MS = 10 * 60 * 1000
# Ф10-4 П3: строка с файлом — лиз не короче 7 мин (у коннектора на файл 300 с, `FILE_SEND_TIMEOUT_S`).
FILE_LEASE_MS = 420_000
# Ф12-2 П1: запланированное касание устарело — в беседе после подтверждения был ответ (человека или владельца).
PACE_STALE_ERROR = "после подтверждения в беседе был ответ — касание не ушло"
# Ф30-1 решение 3: строка с `send_at_ts` позже `send_at_ts + SEND_AT_GRACE_MS` не выдаётся (хаб спал,
# коннектор лежал) — `failed` с этой причиной (не выдававшаяся) или `unknown` (выдававшаяся). Решение 4:
# подтверждение позже того же порога — `SendAtPassed` (409), владелец решает «Отправить сейчас».
SEND_AT_GRACE_MS = 2 * 60 * 60 * 1000
SEND_AT_LATE_ERROR = "send_at_late"
# Ф30-2 решение 2 + П4: строка с `send_at_ts` и `cancel_if_reply` не уходит, если после подтверждения в беседе
# был ответ (`_reply_after_approve`) — своя причина, не «касание» графика.
SEND_AT_REPLIED_ERROR = "после подтверждения в беседе был ответ — догон не ушёл"
# Ф33-3 (К12): строка, бывшая в пути (`queued`/`sending`) в восстановленной копии, — «могло уйти» после снимка
# на прежней машине; держится `unknown` до решения владельца (`hold_after_restore`).
RESTORE_HOLD_ERROR = "восстановлено из копии — могло уйти до сбоя"
_rng = random.SystemRandom()  # Ф12-2: случайный промежуток графика; тесты передают свой `rng`


class OutboxError(Exception):
    """Базовая ошибка автомата исходящих."""


NO_REPLY_ERROR = "беседа трекера: ответ из хаба не уходит — комментарий пишется в самом трекере"


def channel_no_reply(meta_json: str | None) -> bool:
    """Ф10-5 B2: у беседы `meta_json.attrs.no_reply is True` (уведомления трекеров с почты) — ответа из хаба
    нет. Битый JSON/не словарь — ответ разрешён (прежнее поведение)."""
    if not meta_json:
        return False
    try:
        meta = json.loads(meta_json)
    except ValueError, TypeError:
        return False
    attrs = meta.get("attrs") if isinstance(meta, dict) else None
    return isinstance(attrs, dict) and attrs.get("no_reply") is True


class InvalidTransition(OutboxError):
    """Запрошенный переход невозможен из текущего статуса; состояние не изменено."""


class StaleLease(OutboxError):
    """Отчёт пришёл не от текущего владельца лиза (или лиз уже не в sending); состояние не изменено."""


class IdempotencyConflict(OutboxError):
    """`(api_token_id, idempotency_key)` уже занят другим каналом/текстом."""


class NotFound(OutboxError):
    """Канал или строка outbox не найдены."""


class UploadInvalid(OutboxError):
    """Ф10-4 A3/П1: `upload_id` неизвестен, уже использован, принадлежит другой беседе, файл пропал со склада,
    источник беседы файлы не принимает или подпись Telegram длиннее 1024 UTF-16 (П11). Маршрут — 422."""


class DraftChannelBusy(OutboxError):
    """Ф12-1, решение 4: в беседу-заготовку (временный `ext_id`) — только один черновик, пока не уйдёт первое
    сообщение. Маршрут — 409."""


class SendAtPassed(InvalidTransition):
    """Ф30-1 решение 4 + П8: подтверждение черновика с `send_at_ts` позже `send_at_ts + SEND_AT_GRACE_MS` —
    состояние не изменено; маршрут отвечает 409 `send_at_passed` (в пачке — ошибка этого id), UI предлагает
    «Отправить сейчас» (`approve(send_now=True)`). Ф32-2 п.3 + П8: `tz` — пояс аккаунта строки (`pace_tz`),
    в нём маршрут пишет «время отправки ЧЧ:ММ прошло»."""

    def __init__(self, send_at_ts: int, tz=None):
        super().__init__(f"approve: время отправки {send_at_ts} прошло")
        self.send_at_ts = send_at_ts
        self.tz = tz


class Approved(NamedTuple):
    """Ф30-1 П7: итог подтверждения — когда уйдёт и почему не сразу: `mode` `None` (обычная строка, уходит
    сразу, `not_before_ts` не трогается), `'paced'` (слот графика) или `'send_at'` (точное время)."""

    not_before_ts: int | None
    mode: str | None
    send_at_ts: int | None = None


class NotWaiting(InvalidTransition):
    """«Отправить сейчас» (24.10): строка не ждёт своего слота или срока — уже уходит, уже ушла или не в
    очереди. `reason` — короткая причина для владельца (маршрут — 409). Состояние не изменено."""

    def __init__(self, reason: str):
        super().__init__(f"send-now: {reason}")
        self.reason = reason


class NotOwnDraft(OutboxError):
    """Спека 7a, A4/П1: bearer отменяет не свой черновик или черновик уже не `pending_approval` (владелец его
    подтвердил и т.п.). Состояние не изменено; маршрут отвечает 403 и отдельной транзакцией пишет аудит."""


class EnqueueResult(NamedTuple):
    outbox_id: int
    message_id: int
    status: str
    created: bool
    # Ф12-5 П6: фактический «по графику» строки (bearer в беседу-заготовку — всегда `True`), не запрошенный.
    paced: bool = False


class Lease(NamedTuple):
    outbox_id: int
    channel_ext_id: str
    text: str
    reply_to_ext_id: str | None
    thread_ext_id: str | None
    send_token: str
    mode: str  # 'send' | 'reconcile'
    send_started_ts: int
    attempts: int
    lease_gen: int  # поколение лиза: report() обязан вернуть его, иначе StaleLease
    # Находка F1 критика куска 1c (`docs/architecture.md`): `maybe_sent` — единственный сигнал
    # «а мог ли этот лиз реально уйти» (ставится ТОЛЬКО в ветке истёкшего лиза ниже, не сбрасывается ничем,
    # кроме перехода в 'sent'); коннектор ищет в истории ПЕРЕД отправкой только при `maybe_sent=True`, а не
    # по эвристике `attempts > 1` (та ложно триггерилась и после честного temporary/flood, где запрос точно
    # не уходил). `known_own_ext_ids` — ext_id уже известных ядру СВОИХ сообщений этого канала в окне
    # `[send_started_ts-60с, ...]` (не больше 200) — поиск в истории обязан их игнорировать: они уже чьи-то.
    maybe_sent: bool
    known_own_ext_ids: list[str]
    # Ф11-3b A4 (контракт A↔B): `None` или список `{size, mime, name, kind, sha256}` в порядке вложений
    # (1–10); байты — внутренняя ручка `GET /internal/v1/outbox/{outbox_id}/file?lease_gen=N&idx=K`. Текст
    # при файлах может быть пустым (подпись).
    files: list[dict] | None = None
    # Ф12-1 A3: адресат беседы-заготовки (`{"kind": "tg_username", "username"}` |
    # `{"kind": "email", "to", "subject", "from"}`); у обычной беседы — `None`.
    new_to: dict | None = None


def _sql(schema: str, template: str, **parts: str) -> str:
    """Подставить имя схемы (`main`/`work`) в шаблон запроса. См. одноимённую обёртку в ingest.py."""
    return template.format(schema=schema, **parts)


_ext_sort = extsort.ext_sort  # раздел A спеки 2b: единый модуль extsort.py, дубль здесь убран


def _is_idempotent(config_json: str | None) -> bool:
    if not config_json:
        return False
    try:
        return bool(json.loads(config_json).get("idempotent_send", False))
    except ValueError, TypeError, AttributeError:
        return False


def _channel_trusted(
    my_msg_count: int, first_seen_ts: int | None, title_history_json: str | None, now_ms: int
) -> bool:
    """Признаки подлинности канала (раздел 5 плана). Со спеки 7a (A1) `enqueue` на них не опирается — всё
    через bearer идёт черновиком; функция остаётся для будущего прямого режима (`direct` по allow-list)."""
    if my_msg_count == 0:
        return False
    if first_seen_ts is None or now_ms - first_seen_ts < _UNTRUSTED_CHANNEL_AGE_MS:
        return False
    if title_history_json:
        try:
            history = json.loads(title_history_json)
        except ValueError, TypeError:
            history = []
        for entry in history:
            ts = entry.get("ts") if isinstance(entry, dict) else None
            if ts is not None and now_ms - ts < _RECENT_RENAME_MS:
                return False
    return True


def _audit(conn, schema, *, action, outbox_id, channel_id, api_token_id, result, now_ms, detail=None) -> None:
    """audit лежит только в main, независимо от файла канала; текст сообщения сюда никогда не попадает.
    `detail` — только идентификаторы/числа (находка F1(в) критика 1c: `outbox_ext_id_conflict` кладёт сюда
    оба outbox_id), не текст."""
    conn.execute(
        "INSERT INTO main.audit (ts, actor, api_token_id, action, result, channel_id, outbox_id, "
        "detail_json) VALUES (?, 'outbox', ?, ?, ?, ?, ?, ?)",
        (
            now_ms,
            api_token_id,
            action,
            result,
            channel_id,
            outbox_id,
            json.dumps(detail) if detail is not None else None,
        ),
    )


def _known_own_ext_ids(
    conn, schema: str, channel_id: int, *, since_ms: int, idempotent: bool, limit: int = 200
) -> list[str]:
    """Находка F1(б) критика куска 1c: ext_id своих (`is_me`, `ext_id IS NOT NULL`) сообщений этого канала,
    уже известных ядру, начиная с `since_ms` — не больше `limit`. Любое из них уже чьё-то (своей строке
    outbox эхо склеилось бы по `send_token`/тексту+времени — см. `match_echo`/`apply_echo`), поиск в истории
    коннектора обязан их пропускать, иначе он «найдёт» и присвоит себе чужую доставку.

    Поправки после opus-критика кода 2c+2d (`docs/architecture.md`): у
    НЕидемпотентного аккаунта (Slack; `idempotent_send` не задан) — только свои сообщения, УЖЕ привязанные к
    outbox (`outbox_id IS NOT NULL`). Эхо, не склеившееся с заготовкой из-за другого текста (`:+1:` против
    👍 — эхо источника в своей форме), — это как раз искомая доставка: исключить его значило бы ложный
    `unknown` при reconcile и дубль после «Повторить». Идемпотентный (Telegram) — без изменений."""
    outbox_filter = "" if idempotent else "AND outbox_id IS NOT NULL "
    rows = conn.execute(
        _sql(
            schema,
            "SELECT ext_id FROM {schema}.message WHERE channel_id = ? AND is_me = 1 AND ext_id IS NOT NULL "
            "{outbox_filter}AND ts >= ? ORDER BY ts DESC LIMIT ?",
            outbox_filter=outbox_filter,
        ),
        (channel_id, since_ms, limit),
    ).fetchall()
    return [r[0] for r in rows]


def _placeholder_id(conn, schema: str, outbox_id: int) -> int:
    row = conn.execute(
        _sql(schema, "SELECT id FROM {schema}.message WHERE outbox_id = ?"), (outbox_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"no message row for outbox_id {outbox_id}")
    return row[0]


def _bump_seq_by_message_id(conn, schema: str, message_id: int, *, deleted: bool = False) -> int:
    seq = next_seq(conn)
    if deleted:
        conn.execute(
            _sql(schema, "UPDATE {schema}.message SET seq = ?, deleted = 1 WHERE id = ?"), (seq, message_id)
        )
    else:
        conn.execute(_sql(schema, "UPDATE {schema}.message SET seq = ? WHERE id = ?"), (seq, message_id))
    return seq


def text_sha256(text: str) -> str:
    """Ревью Ф8-4: хеш текста исходящего — склейка эха со СКРЫТОЙ заготовкой, чей текст стёрт
    (`match_echo`)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def backfill_text_sha256(conn, schema: str, outbox_id: int) -> None:
    """Строка старше миграции 0012 — хеш из ещё не стёртого текста (зовёт `hiding` перед стиранием)."""
    row = conn.execute(
        _sql(schema, "SELECT text, text_sha256 FROM {schema}.outbox WHERE id = ?"), (outbox_id,)
    ).fetchone()
    if row is not None and row[1] is None and row[0]:
        conn.execute(
            _sql(schema, "UPDATE {schema}.outbox SET text_sha256 = ? WHERE id = ?"),
            (text_sha256(row[0]), outbox_id),
        )


def _placeholder_hidden(conn, schema: str, message_id: int) -> bool:
    """Ф8-4: строка (заготовка или настоящая) удалена владельцем у себя в хабе (`message.hidden`)."""
    row = conn.execute(
        _sql(schema, "SELECT hidden FROM {schema}.message WHERE id = ?"), (message_id,)
    ).fetchone()
    return bool(row[0]) if row is not None else False


def _clear_text_if_hidden(conn, schema: str, outbox_id: int, message_id: int) -> None:
    """Ф8-4 П1: заготовка скрыта, а строка очереди дошла до конечного статуса уже после скрытия (эхо, отчёт
    коннектора, решение владельца по `unknown`) — текст строки очереди стирается тем же правилом, что при самом
    скрытии (`hiding._settle_outbox`): в конечном статусе, строка остаётся журналом отправки. Ф19-1 C: вместе
    с текстом — исходный текст правленого черновика (`draft_text`)."""
    if not _placeholder_hidden(conn, schema, message_id):
        return
    conn.execute(
        _sql(
            schema,
            "UPDATE {schema}.outbox SET text = '', draft_text = NULL "
            "WHERE id = ? AND status IN ('sent', 'cancelled', 'failed')",
        ),
        (outbox_id,),
    )
    scrub_file_json(conn, schema, outbox_id)


def scrub_file_json(conn, schema: str, outbox_id: int) -> None:
    """Ф10-4 П2: «удалить у себя» — у файла строки очереди в конечном статусе остаётся только `{kind, size}`
    (имя, sha256 и `upload_id` стираются вместе с текстом). Зовут `hiding._settle_outbox` и
    `_clear_text_if_hidden`."""
    row = conn.execute(
        _sql(
            schema,
            "SELECT file_json FROM {schema}.outbox "
            "WHERE id = ? AND status IN ('sent', 'cancelled', 'failed')",
        ),
        (outbox_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return
    # Ф11-3b A1: у КАЖДОГО файла остаётся `{kind, size}`; запись — всегда списком.
    kept = [
        {key: value[key] for key in ("kind", "size") if key in value}
        for value in _parse_file_json(row[0]) or []
    ]
    conn.execute(
        _sql(schema, "UPDATE {schema}.outbox SET file_json = ? WHERE id = ?"), (json.dumps(kept), outbox_id)
    )


def _channel_ext_id(conn, schema: str, channel_id: int) -> str:
    return conn.execute(
        _sql(schema, "SELECT ext_id FROM {schema}.channel WHERE id = ?"), (channel_id,)
    ).fetchone()[0]


def _outbox_api_token(conn, schema: str, outbox_id: int) -> int:
    return conn.execute(
        _sql(schema, "SELECT api_token_id FROM {schema}.outbox WHERE id = ?"), (outbox_id,)
    ).fetchone()[0]


def _parse_file_json(raw: str | None) -> list[dict] | None:
    """Ф11-3b A1: `outbox.file_json` -> список словарей файлов или `None` (нет файла, кривое значение). Новая
    запись — список в порядке вложений; старая строка (Ф10-4) — один словарь, читается как список из одного.
    Единственная функция разбора для всех читателей (`enqueue`, `scrub_file_json`, `_lease_files`,
    `outbox_queries.file_for_lease`, `resolve_unknown`, `_lease_ms_for`, `match_echo`)."""
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError, TypeError:
        return None
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        items = [item for item in value if isinstance(item, dict)]
        return items or None
    return None


def _lease_files(raw: str | None) -> list[dict] | None:
    """Поле `files` задания (контракт A↔B): список `{size, mime, name, kind, sha256}` — без служебного
    `upload_id`, в порядке вложений."""
    files = _parse_file_json(raw)
    if files is None:
        return None
    return [{key: value.get(key) for key in ("size", "mime", "name", "kind", "sha256")} for value in files]


def _is_album(file_json: str | None) -> bool:
    """Ф11-3b П4: строка с ≥ 2 файлами. Её `known_own_ext_ids` — только свои сообщения, привязанные к outbox
    (как у неидемпотентного аккаунта): элементы альбома, пришедшие сверкой раньше отчёта (без `outbox_id`), —
    это и есть искомая доставка, поиск альбома в истории не должен их отбрасывать. Прежний альбом исключается
    целиком по своему элементу 0 (он с `outbox_id`)."""
    return len(_parse_file_json(file_json) or []) >= 2


def _lease_ms_for(file_json: str | None, lease_ms: int) -> int:
    """П3 Ф10-4: строке с файлом (любым из списка) — лиз не короче `FILE_LEASE_MS`."""
    return max(lease_ms, FILE_LEASE_MS) if _parse_file_json(file_json) is not None else lease_ms


def _source_of(conn, account_id: int) -> str | None:
    row = conn.execute("SELECT source FROM main.account WHERE id = ?", (account_id,)).fetchone()
    return row[0] if row is not None else None


def _take_upload(conn, *, channel_id: int, account_id: int, upload_id: str) -> dict:
    """A3/A4 + П1 Ф10-4: загрузка этой беседы, файл на складе аккаунта есть. Иначе `UploadInvalid` (422);
    состояние не меняется. Источник беседы и лимиты сообщения проверяет `_check_files`."""
    upload = uploads_mod.get(conn, upload_id=upload_id, channel_id=channel_id)
    if upload is None or upload["account_id"] != account_id:
        raise UploadInvalid("загрузка не найдена или уже отправлена")
    if not media_mod.file_path(media_mod.data_dir_of(conn), account_id, upload["sha256"]).is_file():
        raise UploadInvalid("файл загрузки пропал, прикрепите заново")
    return upload


def _check_files(source: str | None, files: list[dict], text: str) -> None:
    """Ф11-3b решение 3 + П6 (лимиты сообщения, 422): сумма размеров — 50 МиБ (почта — 17 МиБ); Telegram — в
    одном альбоме только картинки или только файлы, подпись не длиннее 1024 UTF-16 (П11 Ф10-4). Раскладку по
    пачкам делает UI, ядро только проверяет."""
    # Ф14-4 A2: голосовое — отдельное сообщение из одного файла.
    if len(files) > 1 and any(item["kind"] == uploads_mod.VOICE_KIND for item in files):
        raise UploadInvalid("голосовое — отдельным сообщением")
    total = sum(item["size"] for item in files)
    if source == "mail":
        if total > uploads_mod.MAIL_MAX_BYTES:
            raise UploadInvalid("письмо: вложения не больше 17 МиБ суммой")
    elif total > uploads_mod.MESSAGE_MAX_BYTES:
        raise UploadInvalid("файлы одного сообщения — не больше 50 МиБ суммой")
    if source == "telegram":
        if len({item["kind"] for item in files}) > 1:
            raise UploadInvalid("в одном альбоме Telegram — только картинки или только файлы")
        if uploads_mod.utf16_len(text) > uploads_mod.TELEGRAM_CAPTION_MAX_UTF16:
            raise UploadInvalid("подпись к файлу в Telegram — не длиннее 1024 символов")


def enqueue(
    conn,
    *,
    channel_id: int,
    text: str,
    reply_to_ext_id: str | None = None,
    thread_ext_id: str | None = None,
    origin: str,
    api_token_id: int,
    idempotency_key: str | None,
    now_ms: int,
    upload_ids: list[str] | None = None,
    paced: bool = False,
    send_at: str | None = None,
    cancel_if_reply: bool = False,
) -> EnqueueResult:
    """Ф10-4 A3/A4 + Ф11-3b A2/A3: `upload_ids` — 1–10 загрузок этой беседы (`uploads`) без повторов, одно
    сообщение источнику; текст при них — подпись и может быть пустым. Порядок проверок (П10 Ф10-4): канал ->
    повтор по ключу идемпотентности (тот же текст И тот же СПИСОК `upload_id` в том же порядке — прежний
    результат, иначе `IdempotencyConflict`, П15) -> каждая загрузка -> лимиты сообщения (`_check_files`).

    Ф12-2 A2: `paced` — черновик «по графику»: подтверждение ставит его в слот аккаунта (`pace.py`), а не
    сразу. Только черновик (`origin='api'`): строка UI уходит в `queued` сразу, графика у неё нет (П3).

    Ф30-1 A2: `send_at` — отправить к этому времени после подтверждения (строка разбирается в поясе
    `pace_tz` аккаунта беседы, `pace.parse_send_at`; кривое/прошлое/дальше 30 дней — `OutboxError`, 422).
    Только черновик существующей беседы, не вместе с `paced` (и не в беседу-заготовку, где график ставится
    сам, П6).

    Ф30-2 решение 1: `cancel_if_reply` — догон к сроку не уходит, если после подтверждения в беседе ответили
    (`lease`); только вместе с `send_at` (иначе `OutboxError`, 422)."""
    if cancel_if_reply and send_at is None:
        raise OutboxError("cancel_if_reply — только вместе с send_at")
    if paced and origin != "api":
        raise OutboxError("по графику — только черновик, который подтверждает владелец")
    if send_at is not None and origin != "api":
        raise OutboxError("send_at: только черновик, который подтверждает владелец")
    if send_at is not None and paced:
        raise OutboxError("send_at: не вместе с paced — точное время и график — разные режимы")
    upload_ids = list(upload_ids) if upload_ids else []
    if text is None or (not upload_ids and not text.strip()):
        raise OutboxError("enqueue: пустой текст")
    if len(upload_ids) > uploads_mod.MAX_FILES_PER_MESSAGE:
        raise UploadInvalid("не больше 10 файлов в одном сообщении")
    if len(set(upload_ids)) != len(upload_ids):
        raise UploadInvalid("один и тот же файл дважды в сообщении")

    schema = db_of(channel_id)
    other_schema = "work" if schema == "main" else "main"

    channel_row = conn.execute(
        _sql(
            schema,
            "SELECT archived, account_id, my_msg_count, first_seen_ts, title_history_json, meta_json, ext_id "
            "FROM {schema}.channel WHERE id = ?",
        ),
        (channel_id,),
    ).fetchone()
    if channel_row is None:
        raise NotFound(f"unknown channel_id {channel_id}")
    (archived, account_id, _my_msg_count, _first_seen_ts, _title_history_json, meta_json,
     channel_ext) = channel_row  # fmt: skip
    if archived:
        raise OutboxError(f"channel {channel_id} archived")
    if channel_no_reply(meta_json):
        raise OutboxError(NO_REPLY_ERROR)

    if idempotency_key is not None:
        existing = conn.execute(
            _sql(
                schema,
                "SELECT o.id, o.channel_id, o.text, o.status, m.id, o.file_json, o.paced "
                "FROM {schema}.outbox o JOIN {schema}.message m ON m.outbox_id = o.id "
                "WHERE o.api_token_id = ? AND o.idempotency_key = ?",
            ),
            (api_token_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            (existing_id, existing_channel_id, existing_text, existing_status, existing_message_id,
             existing_file_json, existing_paced) = existing  # fmt: skip
            existing_upload_ids = [
                item.get("upload_id") for item in _parse_file_json(existing_file_json) or []
            ]
            if (
                existing_channel_id == channel_id
                and existing_text == text
                and existing_upload_ids == upload_ids
            ):
                return EnqueueResult(
                    outbox_id=existing_id,
                    message_id=existing_message_id,
                    status=existing_status,
                    created=False,
                    paced=bool(existing_paced),
                )
            raise IdempotencyConflict(
                f"idempotency_key {idempotency_key!r} уже занят другим каналом/текстом/файлом"
            )

        other_existing = conn.execute(
            _sql(
                other_schema, "SELECT id FROM {schema}.outbox WHERE api_token_id = ? AND idempotency_key = ?"
            ),
            (api_token_id, idempotency_key),
        ).fetchone()
        if other_existing is not None:
            raise IdempotencyConflict(f"idempotency_key {idempotency_key!r} уже занят в другом файле")

    draft_channel = draft_channels.is_temp_ext_id(channel_ext)
    if draft_channel:
        # Ф12-1, решение 4 + Ф12-5 A2: беседа-заготовка — одно первое сообщение за раз: второе — только если
        # живой строки нет (прошлое не ушло: `failed`/`cancelled`). Ф14-2 A4: файлы в первом сообщении — у
        # всех источников с файлами (проверки ниже — общие, как у обычной беседы).
        if draft_channels.has_live(conn, schema, channel_id):
            raise DraftChannelBusy("новая беседа: сначала уйдёт первое сообщение")
        if send_at is not None:
            # Ф30-1 П6 (решение 1): первому сообщению новому человеку — только график Ф12-2.
            raise OutboxError("send_at: не для первого сообщения новому адресату — оно уходит по графику")
        if origin == "api":
            # Ф12-5 П6 (Ф12-2 решение 1): первое касание новому человеку от сессии — всегда по графику.
            paced = True

    send_at_ts = None
    if send_at is not None:
        try:
            send_at_ts = pace.parse_send_at(send_at, pace.settings_for(conn, account_id).tz, now_ms)
        except pace.SendAtInvalid as exc:
            raise OutboxError(str(exc)) from exc

    uploads: list[dict] = []
    file_json = None
    if upload_ids:
        if _source_of(conn, account_id) not in uploads_mod.SOURCES:
            raise UploadInvalid("файлы в эту беседу не отправляются")
        uploads = [
            _take_upload(conn, channel_id=channel_id, account_id=account_id, upload_id=upload_id)
            for upload_id in upload_ids
        ]
        _check_files(_source_of(conn, account_id), uploads, text)
        # Ф11-3b A1/A3: всегда список, в порядке `upload_ids`.
        file_json = json.dumps(
            [
                {
                    "sha256": upload["sha256"],
                    "size": upload["size"],
                    "mime": upload["mime"],
                    "name": upload["name"],
                    "kind": upload["kind"],
                    "upload_id": upload_id,
                }
                for upload_id, upload in zip(upload_ids, uploads, strict=True)
            ],
            ensure_ascii=False,
        )

    if origin == "ui":
        status = "queued"
    elif origin == "api":
        # Спека 7a, A1 (план 5.1, умолчание `confirm`; план 11.9): всё, что пришло через bearer, — черновик,
        # независимо от признаков подлинности канала. Прямой режим — отдельный кусок, по решению владельца.
        status = "pending_approval"
    else:
        raise OutboxError(f"unknown origin {origin!r}")

    send_token = uuid.uuid4().hex
    cur = conn.execute(
        _sql(
            schema,
            "INSERT INTO {schema}.outbox (channel_id, account_id, thread_ext_id, reply_to_ext_id, text, "
            "text_sha256, origin, api_token_id, idempotency_key, send_token, status, not_before_ts, "
            "created_ts, file_json, paced) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ),
        (
            channel_id,
            account_id,
            thread_ext_id,
            reply_to_ext_id,
            text,
            text_sha256(text),
            origin,
            api_token_id,
            idempotency_key,
            send_token,
            status,
            now_ms,
            now_ms,
            file_json,
            1 if paced else 0,
        ),
    )
    outbox_id = cur.lastrowid
    if origin == "ui":
        # Ф70-1 B.1: ручная отправка владельца уходит и пока аккаунт спит (день проекта закрыт).
        set_by_hand(conn, outbox_id, True)
    if send_at_ts is not None:
        # Отдельной записью, а не колонкой INSERT: обычная строка пишется одинаково и до миграции 0043.
        conn.execute(
            _sql(schema, "UPDATE {schema}.outbox SET send_at_ts = ? WHERE id = ?"), (send_at_ts, outbox_id)
        )
    if cancel_if_reply:
        # Ф30-2 (0045) — так же отдельной записью: строка без флажка пишется одинаково и до миграции.
        conn.execute(
            _sql(schema, "UPDATE {schema}.outbox SET cancel_if_reply = 1 WHERE id = ?"), (outbox_id,)
        )

    seq = next_seq(conn)
    attachments_json = None
    if uploads:
        # A4 + П16 Ф10-4, Ф11-3b A3: заготовка — `kind='file'`, по вложению на файл в том же виде, что у
        # приёма Ф8-5, в порядке `upload_ids`.
        attachments_json = json.dumps(
            [
                {
                    "type": upload["kind"],
                    "name": upload["name"],
                    "size": upload["size"],
                    "mime": upload["mime"],
                }
                for upload in uploads
            ],
            ensure_ascii=False,
        )
    message_cur = conn.execute(
        _sql(
            schema,
            "INSERT INTO {schema}.message (seq, first_seq, account_id, channel_id, ext_id, ts, is_me, kind, "
            "text, attachments_json, outbox_id, thread_ext_id, reply_to_ext_id) "
            "VALUES (?, ?, ?, ?, NULL, ?, 1, ?, ?, ?, ?, ?, ?)",
        ),
        (
            seq,
            seq,
            account_id,
            channel_id,
            now_ms,
            "file" if uploads else "text",
            text,
            attachments_json,
            outbox_id,
            thread_ext_id,
            reply_to_ext_id,
        ),
    )
    message_id = message_cur.lastrowid
    for idx, (upload_id, upload) in enumerate(zip(upload_ids, uploads, strict=True)):
        # A4 + П5 Ф10-4, Ф11-3b A3: строки `media` заготовки `idx = 0..N-1` — со всеми полями, что пишет
        # `media.commit_upload` при приёме: владелец сразу видит свои картинки (`GET /media/{id}`), ссылками на
        # файлы склада становятся они (П1). Совпавшие байты двух загрузок — две строки на один файл, ссылки
        # считает `release_file`.
        media_cur = conn.execute(
            _sql(
                schema,
                "INSERT INTO {schema}.media (message_id, account_id, idx, kind, mime, size, name, status, "
                "sha256, stored_size, detected, created_ts, fetched_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?, ?)",
            ),
            (
                message_id,
                account_id,
                idx,
                upload["kind"],
                upload["mime"],
                upload["size"],
                upload["name"],
                upload["sha256"],
                upload["size"],
                upload["detected"],
                now_ms,
                now_ms,
            ),
        )
        # Ф14-6 B: своё голосовое из хаба расшифровывается тоже (решение 5) — строка сразу `ready`.
        transcripts_mod.on_media_ready(conn, media_cur.lastrowid, now_ms=now_ms)
        uploads_mod.delete(conn, upload_id=upload_id, channel_id=channel_id)

    if draft_channel:
        draft_channels.bump(conn, schema, channel_id)  # Ф12-5 П2: `can_reply` беседы-заготовки в `/sync`
    _audit(
        conn,
        schema,
        action="outbox_enqueue",
        outbox_id=outbox_id,
        channel_id=channel_id,
        api_token_id=api_token_id,
        result=status,
        now_ms=now_ms,
    )
    return EnqueueResult(
        outbox_id=outbox_id, message_id=message_id, status=status, created=True, paced=bool(paced)
    )


def set_by_hand(conn, outbox_id: int, by_hand: bool) -> None:
    """Ф70-1 B.1: строку отправил или подтвердил сам владелец кукой интерфейса (`True`) — или нет. Пока аккаунт
    спит, `lease` выдаёт только ручные строки без графика и без срока (`SLEEP_HELD_SQL`)."""
    schema = db_of(outbox_id)
    conn.execute(
        _sql(schema, "UPDATE {schema}.outbox SET by_hand = ? WHERE id = ?"), (1 if by_hand else 0, outbox_id)
    )


def status_of(conn, outbox_id: int) -> str | None:
    """Ф39 B5 + П6: статус строки сейчас (`None` — строки нет) — ручка отмены читает его в своей транзакции ДО
    `cancel`, чтобы назвать исход автору черновика."""
    row = conn.execute(
        _sql(db_of(outbox_id), "SELECT status FROM {schema}.outbox WHERE id = ?"), (outbox_id,)
    ).fetchone()
    return row[0] if row is not None else None


def send_at_of(conn, outbox_id: int) -> int | None:
    """Ф30-1 A2: `send_at_ts` строки (мс UTC) или `None` — для ответа и аудита `POST /messages/send`."""
    row = conn.execute(
        _sql(db_of(outbox_id), "SELECT send_at_ts FROM {schema}.outbox WHERE id = ?"), (outbox_id,)
    ).fetchone()
    return row[0] if row is not None else None


def cancel_if_reply_of(conn, outbox_id: int) -> bool:
    """Ф30-2 A2: флажок «не уйдёт, если ответят» строки — для ответа и аудита `POST /messages/send`."""
    row = conn.execute(
        _sql(db_of(outbox_id), "SELECT cancel_if_reply FROM {schema}.outbox WHERE id = ?"), (outbox_id,)
    ).fetchone()
    return bool(row[0]) if row is not None else False


def has_files(conn, outbox_id: int) -> bool:
    """Ф14-3 A8: у строки есть файлы (`file_json`) — такой черновик пачкой не подтверждается."""
    row = conn.execute(
        _sql(db_of(outbox_id), "SELECT file_json FROM {schema}.outbox WHERE id = ?"), (outbox_id,)
    ).fetchone()
    return row is not None and _parse_file_json(row[0]) is not None


def _norm_draft(text: str) -> str:
    """Ф19-1 B + П5: сравнение «правил ли владелец черновик» — без `\\r\\n` и пробелов по краям."""
    return text.replace("\r\n", "\n").strip()


def draft_edited(conn, outbox_id: int) -> bool:
    """Ф19-1 B: строка подтверждена с правкой текста (`draft_text` записан `approve`; подтверждение у строки
    одно — `pending_approval` назад не возвращается). Для аудита `outbox_approve` ручки."""
    row = conn.execute(
        _sql(db_of(outbox_id), "SELECT draft_text IS NOT NULL FROM {schema}.outbox WHERE id = ?"),
        (outbox_id,),
    ).fetchone()
    return bool(row[0]) if row is not None else False


def approve(conn, outbox_id: int, *, text: str | None = None, now_ms: int, rng=None) -> int | None:
    """Подтверждение черновика -> `queued`; вернёт `not_before_ts` (слот графика или время `send_at`), у
    обычной строки — `None`. Подробности и «Отправить сейчас» — `approve_mode`."""
    return approve_mode(conn, outbox_id, text=text, now_ms=now_ms, rng=rng).not_before_ts


def approve_mode(
    conn,
    outbox_id: int,
    *,
    text: str | None = None,
    now_ms: int,
    rng=None,
    send_now: bool = False,
    audit_action: str = "outbox_approve",
    audit_detail: dict | None = None,
    record_edit: bool = True,
) -> Approved:
    """Подтверждение черновика -> `queued`. Ф12-2 A3: черновик по графику (`paced`) получает слот аккаунта
    (`pace.next_slot`) в `not_before_ts` и `pace_slot_ts` — `Approved(слот, 'paced')`. Обычный — как раньше,
    `Approved(None, None)` (`not_before_ts` не трогается).

    Ф30-1 решения 2 и 4, A3: черновик с `send_at_ts` — `not_before_ts = max(send_at_ts, now)`,
    `Approved(…, 'send_at', send_at_ts)`; окно, промежуток, лимит и «устаревшее касание» графика к нему не
    применяются. Позже `send_at_ts + SEND_AT_GRACE_MS` — `SendAtPassed` (ничего не меняется). `send_now` —
    «Отправить сейчас»: `send_at_ts` обнуляется, дальше обычная отправка; без `send_at_ts` — без эффекта.

    Ф37-1 B3 + П7: `audit_action` — действие записи журнала: подтверждение владельца — `outbox_approve` (по нему
    статистика Ф19-1 узнаёт «подтверждал владелец»), авто-отправка — `outbox_autosend` с `audit_detail` (номер
    подписки, роль, разговор; только идентификаторы). Запись одна — второй `outbox_approve` не пишется.

    Ф56-1а (П9): `record_edit=False` — правка текста при подтверждении не пишется в `draft_text` (правка
    агента владельца в статистику правок владельца Ф19-1 не входит); сам новый текст ставится как обычно."""
    schema = db_of(outbox_id)
    row = conn.execute(
        _sql(
            schema,
            "SELECT status, channel_id, api_token_id, text, paced, account_id, file_json, send_at_ts "
            "FROM {schema}.outbox WHERE id = ?",
        ),
        (outbox_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"unknown outbox_id {outbox_id}")
    status, channel_id, api_token_id, current_text, paced, account_id, file_json, send_at_ts = row
    # Ф14-3 A7 (П7 плана): у черновика с файлами текст — подпись: пустой — можно, правка перепроверяет
    # лимиты сообщения (`_check_files`, Telegram — подпись ≤ 1024 UTF-16).
    files = _parse_file_json(file_json)
    if status != "pending_approval":
        raise InvalidTransition(f"approve: status={status!r}, ожидался pending_approval")
    asked_send_at = send_at_ts
    if send_at_ts is not None and not send_now and now_ms > send_at_ts + SEND_AT_GRACE_MS:
        raise SendAtPassed(send_at_ts, pace.settings_for(conn, account_id).tz)
    channel_meta = conn.execute(
        _sql(schema, "SELECT meta_json FROM {schema}.channel WHERE id = ?"), (channel_id,)
    ).fetchone()
    if channel_meta is not None and channel_no_reply(channel_meta[0]):
        raise OutboxError(NO_REPLY_ERROR)

    placeholder_id = _placeholder_id(conn, schema, outbox_id)
    if text is not None:
        if not text.strip() and files is None:
            raise OutboxError("approve: пустой текст")
        if files is not None:
            _check_files(_source_of(conn, account_id), files, text)
        if record_edit and _norm_draft(text) != _norm_draft(current_text or ""):
            # Ф19-1 B + П7: правка владельца при подтверждении — прежний текст черновика как был (без нормализации)
            # и время правки; правка только пробелов/переводов строк по краям — не правка.
            conn.execute(
                _sql(
                    schema,
                    "UPDATE {schema}.outbox SET draft_text = ?, draft_ts = ? "
                    "WHERE id = ? AND draft_text IS NULL",
                ),
                (current_text, now_ms, outbox_id),
            )
        conn.execute(
            _sql(schema, "UPDATE {schema}.outbox SET text = ?, text_sha256 = ? WHERE id = ?"),
            (text, text_sha256(text), outbox_id),
        )
        # Поправки после opus-критика кода (R7, тест ret8): новый текст на заготовке, УЖЕ очищенной
        # ретенцией (`purged=1`, старый `ts`) — периодический прогон пропускает `purged=1` строки навсегда
        # (раздел F.3: «нечего чистить»), а без сброса флага этот НОВЫЙ текст остался бы в work бессрочно.
        conn.execute(
            _sql(schema, "UPDATE {schema}.message SET text = ?, purged = 0 WHERE id = ?"),
            (text, placeholder_id),
        )
    elif current_text == "" and files is None:
        # Поправки (R7): заготовку стёрла ретенция (пусто, но статус почему-то ещё pending_approval, не
        # исключено при переходе со старой версии) — approve() без НОВОГО текста отправил бы пустое сообщение.
        raise InvalidTransition("approve: текст стёрт по сроку хранения, нужен новый текст")

    slot = None
    mode = None
    if send_at_ts is not None and send_now:
        send_at_ts = None
        conn.execute(_sql(schema, "UPDATE {schema}.outbox SET send_at_ts = NULL WHERE id = ?"), (outbox_id,))
    if send_at_ts is not None:
        mode = "send_at"
        slot = max(send_at_ts, now_ms)
        conn.execute(
            _sql(schema, "UPDATE {schema}.outbox SET status = 'queued', not_before_ts = ? WHERE id = ?"),
            (slot, outbox_id),
        )
    elif paced:
        mode = "paced"
        slot = pace.next_slot(conn, account_id, now_ms, rng or _rng, exclude_id=outbox_id)
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.outbox SET status = 'queued', not_before_ts = ?, pace_slot_ts = ? "
                "WHERE id = ?",
            ),
            (slot, slot, outbox_id),
        )
    else:
        conn.execute(_sql(schema, "UPDATE {schema}.outbox SET status = 'queued' WHERE id = ?"), (outbox_id,))
    _bump_seq_by_message_id(conn, schema, placeholder_id)
    detail: dict | None = None
    if mode is not None:
        detail = {"not_before_ts": slot}
    if asked_send_at is not None:
        detail = {**(detail or {}), "send_at_ts": asked_send_at, "send_now": send_now}
    if audit_detail:
        detail = {**(detail or {}), **audit_detail}
    _audit(
        conn,
        schema,
        action=audit_action,
        outbox_id=outbox_id,
        channel_id=channel_id,
        api_token_id=api_token_id,
        result="queued",
        now_ms=now_ms,
        detail=detail,
    )
    return Approved(slot, mode, send_at_ts)


def cancel(conn, outbox_id: int, *, now_ms: int, owner_token_id: int | None = None) -> str:
    """Вернёт итоговый статус: 'cancelled', либо 'unknown', если строку уже пытались отправить.

    `owner_token_id` задан (отмена от bearer, спека 7a, A4/П1) — отменить можно только СВОЙ черновик
    (`api_token_id` строки = этот токен) в статусе `pending_approval`; иначе `NotOwnDraft`, ничего не
    меняется. Проверка — здесь, в той же транзакции, что и сама отмена: иначе владелец успел бы подтвердить
    черновик (`queued`) между проверкой и отменой, и bearer отменил бы уже подтверждённое."""
    schema = db_of(outbox_id)
    row = conn.execute(
        _sql(
            schema,
            "SELECT status, channel_id, api_token_id, thread_ext_id FROM {schema}.outbox WHERE id = ?",
        ),
        (outbox_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"unknown outbox_id {outbox_id}")
    status, channel_id, api_token_id, thread_ext_id = row
    if owner_token_id is not None and (api_token_id != owner_token_id or status != "pending_approval"):
        raise NotOwnDraft(f"cancel: outbox {outbox_id} — не свой черновик pending_approval ({status!r})")
    if status not in ("pending_approval", "queued"):
        raise InvalidTransition(f"cancel: status={status!r}, ожидались pending_approval|queued")

    placeholder_id = _placeholder_id(conn, schema, outbox_id)
    attempted = conn.execute(
        _sql(schema, "SELECT send_started_ts IS NOT NULL FROM {schema}.outbox WHERE id = ?"), (outbox_id,)
    ).fetchone()[0]
    # Строку уже выдавали коннектору (вернулась в queued после temporary/flood) — она МОГЛА уйти.
    # «Отменено» было бы враньём: повторы останавливаем, но честный статус — unknown, решает человек.
    new_status = "unknown" if attempted else "cancelled"
    conn.execute(
        _sql(schema, "UPDATE {schema}.outbox SET status = ?, lease_owner = NULL WHERE id = ?"),
        (new_status, outbox_id),
    )
    _bump_seq_by_message_id(conn, schema, placeholder_id, deleted=not attempted)
    if not attempted:
        # Ф11-3a C: файл отменённой до попытки отправки больше не нужен — строки `media` заготовки прочь, файл
        # склада — по правилу ссылок. После попытки (`unknown`) не трогаем: файл мог уйти.
        media_rows = conn.execute(
            _sql(schema, "SELECT id, account_id, sha256 FROM {schema}.media WHERE message_id = ?"),
            (placeholder_id,),
        ).fetchall()
        for media_id, media_account_id, sha256 in media_rows:
            conn.execute(_sql(schema, "DELETE FROM {schema}.media WHERE id = ?"), (media_id,))
            media_mod.release_file(conn, schema, media_account_id, sha256)
    # Раздел D спеки 2b: отмена заготовки-ответа тоже пересчитывает сводку треда (её пропало из ленты треда).
    if thread_ext_id is not None:
        threads_mod.touch_and_bump(conn, schema, channel_id, thread_ext_id, now_ms)
    # Ф12-1, решение 6 + П2: отменённый черновик беседы-заготовки — беседа в архив; `unknown` — не трогаем.
    draft_channels.settle(conn, schema, channel_id, status=new_status, outbox_id=outbox_id, now_ms=now_ms)
    _audit(
        conn,
        schema,
        action="outbox_cancel",
        outbox_id=outbox_id,
        channel_id=channel_id,
        api_token_id=api_token_id,
        result=new_status,
        now_ms=now_ms,
    )
    return new_status


def release_now(conn, outbox_id: int, *, now_ms: int) -> None:
    """«Отправить сейчас» у подтверждённой строки: строка `queued`, ещё не выданная
    коннектору (`send_started_ts IS NULL`) и ждущая — по графику (`paced`, Ф12-2) или срока (`send_at_ts` /
    `not_before_ts` впереди, Ф30-1), — уходит при ближайшей выдаче. Клик владельца — его решение: снимаются график
    (`paced = 0`, `pace_slot_ts = NULL` — окно, промежуток и суточный лимит не применяются), срок
    (`send_at_ts = NULL`) и «не уйдёт, если ответят» (`cancel_if_reply = 0`: ответ в беседе догон уже не
    снимает), `not_before_ts = now`, `by_hand = 1` (уходит и пока аккаунт спит, как подтверждённое владельцем).
    Стоп PeerFlood (`peer_flood.held_checker`), FIFO беседы и прочие предохранители `lease` остаются.

    Иначе — `NotWaiting` с причиной (уже уходит / уже ушла / не в очереди / не ждёт)."""
    schema = db_of(outbox_id)
    row = conn.execute(
        _sql(
            schema,
            "SELECT status, channel_id, api_token_id, paced, send_at_ts, not_before_ts, send_started_ts "
            "FROM {schema}.outbox WHERE id = ?",
        ),
        (outbox_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"unknown outbox_id {outbox_id}")
    status, channel_id, api_token_id, paced, send_at_ts, not_before_ts, send_started_ts = row
    if status == "sending" or (status == "queued" and send_started_ts is not None):
        raise NotWaiting("уже уходит")
    if status == "sent":
        raise NotWaiting("уже ушло")
    if status != "queued":
        raise NotWaiting("не в очереди")
    if not paced and send_at_ts is None and (not_before_ts or 0) <= now_ms:
        raise NotWaiting("не ждёт — уходит обычным порядком")
    conn.execute(
        _sql(
            schema,
            "UPDATE {schema}.outbox SET paced = 0, pace_slot_ts = NULL, send_at_ts = NULL, "
            "cancel_if_reply = 0, not_before_ts = ?, by_hand = 1 WHERE id = ?",
        ),
        (now_ms, outbox_id),
    )
    _bump_seq_by_message_id(conn, schema, _placeholder_id(conn, schema, outbox_id))
    _audit(
        conn,
        schema,
        action="outbox_send_now",
        outbox_id=outbox_id,
        channel_id=channel_id,
        api_token_id=api_token_id,
        result="queued",
        now_ms=now_ms,
        detail={
            "paced": bool(paced),
            "send_at_ts": send_at_ts,
            "not_before_ts": not_before_ts,
        },
    )


# Ф12-2 П1: строка графика, чей слот ещё впереди, беседу не держит — ответ владельца в этой беседе уходит сразу.
# Ф30-1 П1: так же подтверждённая строка с `send_at_ts` впереди — по `send_at_ts`, а не по `not_before_ts`
# (строка в бэкоффе после temporary, чьё время уже наступило, порядок держит).
# Общий фрагмент для `lease` и `outbox_queries.has_leasable` (условие выдачи у них одно); параметры —
# `fifo_params`.
FIFO_BLOCKERS_SQL = (
    "SELECT 1 FROM {schema}.outbox WHERE channel_id = ? "
    "AND status IN ('sending', 'queued', 'unknown') "
    "AND NOT (paced = 1 AND status = 'queued' AND not_before_ts > ?) "
    "AND NOT (status = 'queued' AND COALESCE(send_at_ts, 0) > ?) "
    "AND id < ?"
)


def fifo_params(channel_id: int, now_ms: int, outbox_id: int) -> tuple[int, int, int, int]:
    """Параметры `FIFO_BLOCKERS_SQL` по порядку."""
    return (channel_id, now_ms, now_ms, outbox_id)


# Ф70-1 B.1: пока аккаунт спит (день его проекта закрыт, `conn_sleep.asleep`), выдаётся только то, что владелец
# отправил или подтвердил сам кукой интерфейса, без графика и без срока. Остальное ждёт начала дня — и беседу
# при этом НЕ держит: ручная отправка той же беседы уходит раньше ждущей строки (FIFO при сне — по ручным).
# `sending`/`unknown` держат беседу и при сне (строка уже в пути или «могло уйти»).
SLEEP_HELD_SQL = "(by_hand = 0 OR paced = 1 OR send_at_ts IS NOT NULL)"
_FIFO_SLEEP_EXTRA_SQL = " AND NOT (status = 'queued' AND " + SLEEP_HELD_SQL + ")"


def sleep_held(by_hand, paced, send_at_ts) -> bool:
    """Строка ждёт начала дня, пока аккаунт спит (то же, что `SLEEP_HELD_SQL`)."""
    return not by_hand or bool(paced) or send_at_ts is not None


def fifo_sql(sleeping: bool) -> str:
    """`FIFO_BLOCKERS_SQL` (параметры — `fifo_params`); при сне аккаунта ждущие строки беседу не держат."""
    return FIFO_BLOCKERS_SQL + (_FIFO_SLEEP_EXTRA_SQL if sleeping else "")


def account_sleeping(conn, account_id: int, now_ms: int) -> bool:
    """Ф70-1: аккаунт спит — день проекта, в который он идёт целиком, закрыт."""
    return conn_sleep.asleep(conn, account_id, now_ms) is not None


def send_at_late(send_at_ts: int | None, now_ms: int) -> bool:
    """Ф30-1 решение 3: строка с `send_at_ts` опоздала больше чем на `SEND_AT_GRACE_MS` — не отправляется."""
    return send_at_ts is not None and now_ms > send_at_ts + SEND_AT_GRACE_MS


class SleptLate:
    """Ф70-1: опоздание `send_at` с поправкой на сон аккаунта. Срок пришёлся на сон (строка ждала начала дня:
    срок не позже пробуждения и к засыпанию ещё не опоздал) — окно `SEND_AT_GRACE_MS` отсчитывается от
    пробуждения. Последний сон аккаунта читается лениво — только для строки, опоздавшей по старому правилу,
    один раз на вызов `lease`/`has_leasable`."""

    def __init__(self, conn, account_id: int) -> None:
        self._conn = conn
        self._account_id = account_id
        self._slept: tuple[int, int] | None = None
        self._read = False

    def __call__(self, send_at_ts: int | None, now_ms: int) -> bool:
        if not send_at_late(send_at_ts, now_ms):
            return False
        if not self._read:
            self._slept = conn_sleep.last_sleep(self._conn, self._account_id)
            self._read = True
        if self._slept is None:
            return True
        start, end = self._slept
        if send_at_ts > end or send_at_ts + SEND_AT_GRACE_MS < start:
            return True
        return now_ms > end + SEND_AT_GRACE_MS


def _approved_ts(conn, outbox_id: int, fallback: int) -> int:
    """Когда черновик подтвердили: последняя запись `outbox_approve` журнала (append-only, в `main`)."""
    row = conn.execute(
        "SELECT MAX(ts) FROM main.audit WHERE action = 'outbox_approve' AND outbox_id = ?", (outbox_id,)
    ).fetchone()
    return row[0] if row is not None and row[0] is not None else fallback


def _touch_stale(
    conn, schema: str, channel_id: int, outbox_id: int, created_ts: int, *, skip_api: bool = False
) -> bool:
    """Ф12-2 П1: в существующей беседе после подтверждения касания появилось настоящее сообщение — входящее
    человека (не служебное событие `kind='event'`) или своё сообщение владельца (из хаба или с телефона). Ревью
    Ф12-2, находка 1: ответ владельца из хаба считается и пока он ещё в очереди (flood, коннектор выключен, сон) —
    строка outbox `origin='ui'`, созданная после подтверждения касания, в любом статусе, кроме отменённого и
    неотправленного. Беседа-заготовка (Ф12-1) — не проверяется: в ней до отправки сообщений нет.

    Ф30-2 П1: `skip_api` — сообщения строк outbox `origin='api'` (черновики сессий, подтверждённые владельцем, —
    например, письмо, положенное вместе с догоном) ответом не считаются; ответ владельца с телефона (`outbox_id IS
    NULL`) и из UI (`origin='ui'`) — ответ. График (`paced`) зовёт без него — поведение прежнее.

    Ф32-2 п.6б + П9: при `skip_api` служебное событие — не ответ, чьё бы оно ни было (своё закрепление
    владельца, `is_me=1 AND kind='event'`, догон не снимает); у графика своё событие по-прежнему считается."""
    if draft_channels.is_temp_ext_id(_channel_ext_id(conn, schema, channel_id)):
        return False
    since = _approved_ts(conn, outbox_id, created_ts)
    if skip_api:
        row = conn.execute(
            _sql(
                schema,
                "SELECT 1 FROM {schema}.message WHERE channel_id = ? AND ts > ? AND ext_id IS NOT NULL "
                "AND (outbox_id IS NULL OR outbox_id IN "
                "(SELECT id FROM {schema}.outbox WHERE origin <> 'api')) "
                "AND kind <> 'event' LIMIT 1",
            ),
            (channel_id, since),
        ).fetchone()
    else:
        row = conn.execute(
            _sql(
                schema,
                "SELECT 1 FROM {schema}.message WHERE channel_id = ? AND ts > ? AND ext_id IS NOT NULL "
                "AND (outbox_id IS NULL OR outbox_id <> ?) AND (is_me = 1 OR kind <> 'event') LIMIT 1",
            ),
            (channel_id, since, outbox_id),
        ).fetchone()
    if row is not None:
        return True
    reply = conn.execute(
        _sql(
            schema,
            "SELECT 1 FROM {schema}.outbox WHERE channel_id = ? AND id <> ? AND origin = 'ui' "
            "AND created_ts > ? AND status IN ('queued', 'sending', 'unknown', 'sent') LIMIT 1",
        ),
        (channel_id, outbox_id, since),
    ).fetchone()
    return reply is not None


def _settle_stale_touch(
    conn,
    schema: str,
    outbox_id: int,
    channel_id: int,
    attempted: bool,
    now_ms: int,
    *,
    error: str = PACE_STALE_ERROR,
    action: str = "outbox_pace_stale",
) -> None:
    """П1: устаревшее касание не уходит. Не выдавалось коннектору — `failed` с причиной; уже выдавалось
    (вернулось в очередь после temporary/flood) — `unknown`, как у `cancel`: могло уйти, решает владелец.
    Ф30-1 решение 3: тем же путём — опоздавшая строка с `send_at_ts` (`error`/`action` — свои)."""
    new_status = "unknown" if attempted else "failed"
    conn.execute(
        _sql(
            schema,
            "UPDATE {schema}.outbox SET status = ?, error = ?, lease_owner = NULL, lease_expires_ts = NULL "
            "WHERE id = ?",
        ),
        (new_status, error, outbox_id),
    )
    placeholder_id = _placeholder_id(conn, schema, outbox_id)
    _bump_seq_by_message_id(conn, schema, placeholder_id)
    if new_status == "failed":
        draft_channels.settle(conn, schema, channel_id, status=new_status, outbox_id=outbox_id, now_ms=now_ms)
    _clear_text_if_hidden(conn, schema, outbox_id, placeholder_id)
    _audit(
        conn,
        schema,
        action=action,
        outbox_id=outbox_id,
        channel_id=channel_id,
        api_token_id=_outbox_api_token(conn, schema, outbox_id),
        result=new_status,
        now_ms=now_ms,
    )


def _route_auto(conn, outbox_id: int) -> bool:
    """Ф66-1 (П4): последнее подтверждение строки — «сразу да» таблицы маршрута (аудит `outbox_approve` с
    `source = auto`; сессии не виден). Такая строка с `send_at` или графиком при выдаче снова проходит
    страховку беседы с ботом, как авто-отправка."""
    row = conn.execute(
        "SELECT json_extract(detail_json, '$.source') FROM main.audit WHERE action = 'outbox_approve' "
        "AND outbox_id = ? ORDER BY id DESC LIMIT 1",
        (outbox_id,),
    ).fetchone()
    return row is not None and row[0] == "auto"


def _bot_guard_demote(conn, schema: str, outbox_id: int, channel_id: int, now_ms: int) -> bool:
    """Ф63-2, раздел 8 спеки: строка, подтверждённая без владельца (`auto_role`) и ждавшая срока (`send_at`) или
    слота графика, в беседе с ботом перед ПЕРВОЙ выдачей снова проходит страховку по сообщениям бота на этот
    момент. Сработала — назад в `pending_approval` (как черновик со страховкой при создании: без отметок
    авто-отправки; пометку карточки выдача вычисляет сама) и запись `bot_guard` с путём `lease`; `True` —
    строка не выдаётся."""
    from . import bot_guard  # noqa: PLC0415 — bot_guard -> channels -> outbox: цикл импорта

    hit = bot_guard.outbox_category(conn, outbox_id, now_ms=now_ms)
    if hit is None:
        return False
    conn.execute(
        _sql(
            schema,
            "UPDATE {schema}.outbox SET status = 'pending_approval', auto_role = NULL, auto_session = NULL, "
            "pace_slot_ts = NULL WHERE id = ?",
        ),
        (outbox_id,),
    )
    _bump_seq_by_message_id(conn, schema, _placeholder_id(conn, schema, outbox_id))
    bot_guard.audit_hit(
        conn, category=hit, channel_id=channel_id, path="lease", now_ms=now_ms, outbox_id=outbox_id
    )
    return True


def _reslot(conn, schema: str, account_id: int, outbox_id: int, channel_id: int, reason: str, now_ms: int,
            rng, settings) -> None:  # fmt: skip
    """П2: строку графика сейчас выдавать нельзя — новый слот (`pace_slot_ts` и `not_before_ts`). Ревью Ф12-2,
    находка 2: упёрлась в промежуток или в отправку другой строки — промежуток отсчитывается от последней
    отправки (или «сейчас»), а не от самого позднего слота аккаунта; окно и лимит — те же."""
    after = None
    if reason == "in_flight":
        after = now_ms
    elif reason == "gap":
        after = max(now_ms - settings.gap_min_s * 1000, pace.last_sent_ts(conn, account_id, outbox_id) or 0)
    slot = pace.next_slot(
        conn, account_id, now_ms, rng, exclude_id=outbox_id, settings=settings, after_ms=after
    )
    if slot <= now_ms:
        slot = now_ms + settings.gap_min_s * 1000  # страховка от кручения: слот всегда впереди
    conn.execute(
        _sql(schema, "UPDATE {schema}.outbox SET not_before_ts = ?, pace_slot_ts = ? WHERE id = ?"),
        (slot, slot, outbox_id),
    )
    _bump_seq_by_message_id(conn, schema, _placeholder_id(conn, schema, outbox_id))
    _audit(
        conn,
        schema,
        action="outbox_pace_reslot",
        outbox_id=outbox_id,
        channel_id=channel_id,
        api_token_id=_outbox_api_token(conn, schema, outbox_id),
        result="queued",
        now_ms=now_ms,
        detail={"reason": reason, "not_before_ts": slot},
    )


def lease(
    conn,
    *,
    account_id: int,
    owner: str,
    now_ms: int,
    lease_ms: int = 60_000,
    limit: int = 10,
    rng=None,
) -> list[Lease]:
    """Ф12-2: строка графика (`paced`) перед выдачей проверяется ещё раз — устарело ли касание (П1: после
    подтверждения в беседе был ответ -> `failed`) и можно ли отправлять СЕЙЧАС (П2: окно, отправка другой
    строки графика, промежуток, суточный лимит -> новый слот, строка не выдаётся). Строки графика берутся по
    одной: вторая за тот же вызов упрётся в «уже отправляется» и получит слот позже."""
    account_row = conn.execute(
        "SELECT db, flood_until, config_json, status, peer_flood_ts FROM main.account WHERE id = ?",
        (account_id,),
    ).fetchone()
    if account_row is None:
        raise NotFound(f"unknown account_id {account_id}")
    schema, flood_until, config_json, status, peer_flood_ts = account_row
    idempotent = _is_idempotent(config_json)
    # Ф69-1 C + П1: стоп первых сообщений новым людям — строки таких бесед не выдаются и не трогаются.
    held_new_peer = peer_flood_mod.held_checker(conn, schema, peer_flood_ts)

    leases: list[Lease] = []
    if status in ("limited", "auth_required"):
        # Защита от коннектора, который не уважает свой собственный статус (раздел C спеки куска 1a):
        # строки остаются как есть, ничего не выдаём и не трогаем — их подхватит первый lease() после
        # восстановления (resume/повторный вход).
        return leases
    remaining = limit
    pace_settings = None
    # Ф70-1 B.1: аккаунт спит (день его проекта закрыт) — только ручные строки владельца без графика и срока.
    sleeping = account_sleeping(conn, account_id, now_ms)
    late = SleptLate(conn, account_id)  # Ф70-1: срок, пришедшийся на сон, опаздывает от пробуждения

    if remaining > 0 and (flood_until is None or flood_until <= now_ms):
        fresh = conn.execute(
            _sql(
                schema,
                "SELECT id, channel_id, thread_ext_id, reply_to_ext_id, text, send_token, attempts, "
                "send_started_ts, lease_gen, maybe_sent, file_json, paced, created_ts, send_at_ts, "
                "cancel_if_reply, auto_role, by_hand FROM {schema}.outbox WHERE account_id = ? "
                "AND status = 'queued' AND not_before_ts <= ? "
                "ORDER BY id",
            ),
            (account_id, now_ms),
        ).fetchall()
        for (
            outbox_id,
            channel_id,
            thread_ext_id,
            reply_to_ext_id,
            text,
            send_token,
            attempts,
            send_started_ts,
            lease_gen,
            maybe_sent,
            file_json,
            paced,
            created_ts,
            send_at_ts,
            cancel_if_reply,
            auto_role,
            by_hand,
        ) in fresh:
            if remaining <= 0:
                break
            if sleeping and sleep_held(by_hand, paced, send_at_ts):
                # Ф70-1 B.1 (до опоздания, FIFO и графика): ждёт начала дня нетронутой — без нового слота,
                # без «опоздал» и аудита; после «Начать день» проходит обычный порядок.
                continue
            if late(send_at_ts, now_ms):
                # Ф30-1 решение 3 (до FIFO): хаб опоздал больше чем на 2 ч — не уходит. Ревью Ф30-1: «могло
                # уйти» (`unknown`, держит беседу до решения владельца) — только при `maybe_sent` (лиз истёк без
                # отчёта); после `temporary`/`flood` коннектор сказал «не ушло» -> `failed`.
                _settle_stale_touch(
                    conn, schema, outbox_id, channel_id, bool(maybe_sent), now_ms,
                    error=SEND_AT_LATE_ERROR, action="outbox_send_at_late",
                )  # fmt: skip
                continue
            if held_new_peer(channel_id):
                # Ф69-1 П1: ДО FIFO и до графика — задержанная строка не получает новый слот (`_reslot`),
                # аудит и сдвиг `seq` на каждой выдаче. Остаётся `queued`; ветку истёкшего лиза стоп не
                # трогает.
                continue
            blocked = conn.execute(
                _sql(schema, fifo_sql(sleeping)), fifo_params(channel_id, now_ms, outbox_id)
            ).fetchone()
            if blocked is not None:
                continue
            if (
                send_at_ts is not None
                and cancel_if_reply
                and _touch_stale(conn, schema, channel_id, outbox_id, created_ts, skip_api=True)
            ):
                # Ф30-2 решение 2 + П1–П4: догон к сроку, а в беседе после подтверждения ответили — не уходит.
                # После FIFO (П2), как у графика: исход не зависит от того, ради какой строки позван `lease`.
                # «Могло уйти» (`unknown`) — только при `maybe_sent` (П3, как опоздание).
                _settle_stale_touch(
                    conn, schema, outbox_id, channel_id, bool(maybe_sent), now_ms,
                    error=SEND_AT_REPLIED_ERROR, action="outbox_send_at_replied",
                )  # fmt: skip
                continue
            if (
                send_started_ts is None
                and (send_at_ts is not None or paced)
                and (auto_role is not None or _route_auto(conn, outbox_id))
                and _bot_guard_demote(conn, schema, outbox_id, channel_id, now_ms)
            ):
                # Ф63-2, раздел 8 спеки: отложенная авто-отправка в беседу с ботом — страховка по сообщениям
                # бота на момент выдачи; сработала — карточка владельцу.
                continue
            if paced:
                if _touch_stale(conn, schema, channel_id, outbox_id, created_ts):
                    _settle_stale_touch(
                        conn, schema, outbox_id, channel_id, send_started_ts is not None, now_ms
                    )
                    continue
                if pace_settings is None:
                    pace_settings = pace.settings_for(conn, account_id)
                reason = pace.blocked_reason(conn, account_id, outbox_id, now_ms, settings=pace_settings)
                if reason is not None:
                    _reslot(
                        conn, schema, account_id, outbox_id, channel_id, reason, now_ms, rng or _rng,
                        pace_settings,
                    )  # fmt: skip
                    continue

            new_attempts = attempts + 1
            effective_started = send_started_ts if send_started_ts is not None else now_ms
            conn.execute(
                _sql(
                    schema,
                    "UPDATE {schema}.outbox SET status = 'sending', reconcile = 0, lease_owner = ?, "
                    "lease_expires_ts = ?, send_started_ts = COALESCE(send_started_ts, ?), attempts = ?, "
                    "lease_gen = lease_gen + 1 WHERE id = ?",
                ),
                (owner, now_ms + _lease_ms_for(file_json, lease_ms), now_ms, new_attempts, outbox_id),
            )
            placeholder_id = _placeholder_id(conn, schema, outbox_id)
            _bump_seq_by_message_id(conn, schema, placeholder_id)
            _audit(
                conn,
                schema,
                action="outbox_lease",
                outbox_id=outbox_id,
                channel_id=channel_id,
                api_token_id=_outbox_api_token(conn, schema, outbox_id),
                result="sending",
                now_ms=now_ms,
            )
            known_own_ext_ids = _known_own_ext_ids(
                conn,
                schema,
                channel_id,
                since_ms=effective_started - _ECHO_WINDOW_MS,
                idempotent=idempotent and not _is_album(file_json),
            )
            leases.append(
                Lease(
                    outbox_id,
                    _channel_ext_id(conn, schema, channel_id),
                    text,
                    reply_to_ext_id,
                    thread_ext_id,
                    send_token,
                    "send",
                    effective_started,
                    new_attempts,
                    lease_gen + 1,
                    bool(maybe_sent),
                    known_own_ext_ids,
                    _lease_files(file_json),
                    draft_channels.lease_new_to(conn, schema, channel_id),
                )
            )
            remaining -= 1

    stale = conn.execute(
        _sql(
            schema,
            "SELECT id, channel_id, thread_ext_id, reply_to_ext_id, text, send_token, attempts, "
            "send_started_ts, lease_gen, file_json, paced, send_at_ts FROM {schema}.outbox "
            "WHERE account_id = ? AND status = 'sending' AND lease_expires_ts < ? ORDER BY id",
        ),
        (account_id, now_ms),
    ).fetchall()
    for (
        outbox_id,
        channel_id,
        thread_ext_id,
        reply_to_ext_id,
        text,
        send_token,
        attempts,
        send_started_ts,
        lease_gen,
        file_json,
        paced,
        send_at_ts,
    ) in stale:
        # Находка F1(а) критика куска 1c: лиз истёк БЕЗ report() — падение процесса, `abandon`, таймаут
        # каркаса — исход неизвестен и остаётся неизвестным НАВСЕГДА, пока строка не станет 'sent'. Ставим
        # здесь и только здесь; ничего, кроме outcome='sent', этот флаг не снимает.
        conn.execute(_sql(schema, "UPDATE {schema}.outbox SET maybe_sent = 1 WHERE id = ?"), (outbox_id,))

        if attempts > _MAX_ATTEMPTS:
            # Всегда unknown, даже для идемпотентных: падать могли ПОСЛЕ отправки, «failed» было бы враньём.
            final_status = "unknown"
            conn.execute(
                _sql(
                    schema,
                    "UPDATE {schema}.outbox SET status = ?, lease_expires_ts = NULL WHERE id = ?",
                ),
                (final_status, outbox_id),
            )
            placeholder_id = _placeholder_id(conn, schema, outbox_id)
            _bump_seq_by_message_id(conn, schema, placeholder_id)
            _audit(
                conn,
                schema,
                action="outbox_lease_exhausted",
                outbox_id=outbox_id,
                channel_id=channel_id,
                api_token_id=_outbox_api_token(conn, schema, outbox_id),
                result=final_status,
                now_ms=now_ms,
            )
            continue

        if idempotent and send_at_late(send_at_ts, now_ms):
            # Ф30-1 П4: повторная ОТПРАВКА строки с `send_at_ts`, опоздавшей больше чем на 2 ч, не идёт — она
            # уже выдавалась (могла уйти): `unknown`, решает владелец («Повторить»/«Не ушло»). Сверка
            # неидемпотентного (только чтение) — как раньше.
            _settle_stale_touch(
                conn, schema, outbox_id, channel_id, True, now_ms,
                error=SEND_AT_LATE_ERROR, action="outbox_send_at_late",
            )  # fmt: skip
            continue
        if remaining <= 0:
            continue
        # Повторная ОТПРАВКА во время FLOOD_WAIT запрещена, как и первая; сверка — только чтение, ей можно.
        if idempotent and flood_until is not None and flood_until > now_ms:
            continue
        if idempotent and paced:
            # Ревью Ф12-2, находка 3: повторная ОТПРАВКА касания вне окна или сверх лимита дня не идёт —
            # строка возвращается в очередь с новым слотом; `maybe_sent` уже стоит — выдача в слоте сначала
            # поищет своё сообщение в истории (дубля нет). Сверка неидемпотентного (чтение) — как раньше.
            if pace_settings is None:
                pace_settings = pace.settings_for(conn, account_id)
            reason = pace.window_or_daily_reason(conn, account_id, outbox_id, now_ms, settings=pace_settings)
            if reason is not None:
                conn.execute(
                    _sql(
                        schema,
                        "UPDATE {schema}.outbox SET status = 'queued', lease_owner = NULL, "
                        "lease_expires_ts = NULL WHERE id = ?",
                    ),
                    (outbox_id,),
                )
                _reslot(
                    conn,
                    schema,
                    account_id,
                    outbox_id,
                    channel_id,
                    reason,
                    now_ms,
                    rng or _rng,
                    pace_settings,
                )
                continue

        new_attempts = attempts + 1
        mode = "send" if idempotent else "reconcile"
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.outbox SET status = 'sending', reconcile = ?, lease_owner = ?, "
                "lease_expires_ts = ?, attempts = ?, lease_gen = lease_gen + 1 WHERE id = ?",
            ),
            (
                0 if idempotent else 1,
                owner,
                now_ms + _lease_ms_for(file_json, lease_ms),
                new_attempts,
                outbox_id,
            ),
        )
        placeholder_id = _placeholder_id(conn, schema, outbox_id)
        _bump_seq_by_message_id(conn, schema, placeholder_id)
        _audit(
            conn,
            schema,
            action="outbox_lease",
            outbox_id=outbox_id,
            channel_id=channel_id,
            api_token_id=_outbox_api_token(conn, schema, outbox_id),
            result="sending",
            now_ms=now_ms,
        )
        known_own_ext_ids = _known_own_ext_ids(
            conn,
            schema,
            channel_id,
            since_ms=send_started_ts - _ECHO_WINDOW_MS,
            idempotent=idempotent and not _is_album(file_json),
        )
        leases.append(
            Lease(
                outbox_id,
                _channel_ext_id(conn, schema, channel_id),
                text,
                reply_to_ext_id,
                thread_ext_id,
                send_token,
                mode,
                send_started_ts,
                new_attempts,
                lease_gen + 1,
                True,  # maybe_sent — только что проставлен выше, независимо от исхода этой ветки
                known_own_ext_ids,
                _lease_files(file_json),  # П14 Ф10-4: и в ветке истёкшего лиза (reconcile — имя и размер)
                draft_channels.lease_new_to(conn, schema, channel_id),  # Ф12-1 A3: и reconcile новой беседы
            )
        )
        remaining -= 1

    return leases


def _conflicting_outbox_id(
    conn, schema: str, channel_id: int, placeholder_id: int, ext_id: str, outbox_id: int
) -> int | None:
    """Находка F1(в) критика куска 1c: `(channel_id, ext_id)` уже занят ЧУЖИМ outbox — значит найденное
    сообщение на самом деле принадлежит другой нашей отправке (гонка двух почти одновременных строк с
    похожим текстом, не эхо ЭТОЙ), а не подтверждение текущей. Возвращает id владельца, либо `None`
    (свободно — законное позднее эхо, `_report_sent` перевешивает как раньше)."""
    row = conn.execute(
        _sql(
            schema,
            "SELECT outbox_id FROM {schema}.message WHERE channel_id = ? AND ext_id = ? AND id <> ?",
        ),
        (channel_id, ext_id, placeholder_id),
    ).fetchone()
    if row is None or row[0] is None or row[0] == outbox_id:
        return None
    return row[0]


def _move_placeholder_media(conn, schema: str, placeholder_id: int, real_id: int) -> None:
    """Ф10-4 П6: эхо файла пришло отдельной строкой раньше `ts` отчёта (Slack без подписи) — строки `media`
    гаснущей заготовки перевешиваются на настоящую строку, если у неё нет строки с тем же `idx`; иначе
    удаляются (файл — по правилу ссылок П2). Осиротевших строк `media` у погашенной заготовки не остаётся.
    Настоящая строка удалена владельцем у себя (`hidden`) — не перевешивается ничего: удалённое не хранится."""
    rows = conn.execute(
        _sql(schema, "SELECT id, idx, account_id, sha256 FROM {schema}.media WHERE message_id = ?"),
        (placeholder_id,),
    ).fetchall()
    real_hidden = _placeholder_hidden(conn, schema, real_id)
    for media_id, idx, account_id, sha256 in rows:
        taken = conn.execute(
            _sql(schema, "SELECT 1 FROM {schema}.media WHERE message_id = ? AND idx = ?"), (real_id, idx)
        ).fetchone()
        if taken is None and not real_hidden:
            conn.execute(
                _sql(schema, "UPDATE {schema}.media SET message_id = ? WHERE id = ?"), (real_id, media_id)
            )
        else:
            conn.execute(_sql(schema, "DELETE FROM {schema}.media WHERE id = ?"), (media_id,))
            media_mod.release_file(conn, schema, account_id, sha256)


_ALBUM_SOURCES = frozenset({"telegram", "fake"})  # Ф11-3b A5: `fake` — источник стенда
_ALBUM_ID_MAX = 64


class Album(NamedTuple):
    """Ф11-3b A5: годный отчёт об альбоме — `ext_ids` всех N сообщений (`ext_ids[0] == ext_id`) и `grouped_id`
    (не пришёл — `"out:<outbox_id>"`)."""

    ext_ids: list[str]
    grouped_id: str


def _album_from_report(
    conn, schema: str, *, outbox_id: int, account_id: int, outcome: str, ext_id: str | None,
    ext_ids: object, grouped_id: object,
) -> tuple[Album | None, str | None]:  # fmt: skip
    """A5: `(Album, None)` — раскладывать; `(None, причина)` — негодно (отчёт принимается, раскладки нет)."""
    if outcome != "sent":
        return None, "outcome"
    if _source_of(conn, account_id) not in _ALBUM_SOURCES:
        return None, "source"
    row = conn.execute(
        _sql(schema, "SELECT file_json FROM {schema}.outbox WHERE id = ?"), (outbox_id,)
    ).fetchone()
    n_files = len(_parse_file_json(row[0] if row is not None else None) or [])
    if n_files < 2:
        return None, "files"
    if not isinstance(ext_ids, list) or len(ext_ids) != n_files:
        return None, "count"
    if not all(isinstance(item, str) and item and len(item) <= _ALBUM_ID_MAX for item in ext_ids):
        return None, "ext_ids"
    if len(set(ext_ids)) != len(ext_ids) or ext_ids[0] != ext_id:
        return None, "ext_ids"
    if grouped_id is not None and (
        not isinstance(grouped_id, str) or not grouped_id or len(grouped_id) > _ALBUM_ID_MAX
    ):
        return None, "grouped_id"
    return Album(list(ext_ids), grouped_id if grouped_id is not None else f"out:{outbox_id}"), None


def _apply_album(
    conn, schema: str, *, outbox_id: int, placeholder_id: int, element0_id: int, channel_id: int,
    album: Album, now_ms: int, ts_ms: int | None = None,
) -> None:  # fmt: skip
    """Ф11-3b A5 + П3: заготовка с N вложениями раскладывается на N строк альбома — ДО переноса строк `media`
    заготовки на настоящую строку элемента 0 (`_move_placeholder_media`), чтобы на неё ушла только `idx = 0`.

    Элементы k = 1..N-1: строка `(channel_id, ext_ids[k])` уже есть (сверка успела) — строка `media` заготовки
    `idx=k` перевешивается на неё как `idx=0`, если у той нет `idx=0` и она не удалена владельцем у себя
    (`hidden`), иначе удаляется (файл — по правилу ссылок); её `grouped_id` не трогается. Нет — новая строка
    `message` (`is_me`, `kind='file'`, пустой текст, [вложение k], тред/ответ и `ts` заготовки, без
    `outbox_id`), `media idx=k` уходит на неё как `idx=0`. Ф40-1 A: отчёт принёс время источника (`ts_ms`) —
    новые строки получают его (одно на все элементы, как и заготовка в `_report_sent`).
    Элемент 0: заготовка — [вложение 0] и `grouped_id`; настоящая строка из эха — только `grouped_id` (её
    вложения — правда источника). `my_msg_count` и канал — как у обычного `sent` (считает `_report_sent`)."""
    ph_ts, ph_thread, ph_reply, ph_account, ph_attachments = conn.execute(
        _sql(
            schema,
            "SELECT ts, thread_ext_id, reply_to_ext_id, account_id, attachments_json FROM {schema}.message "
            "WHERE id = ?",
        ),
        (placeholder_id,),
    ).fetchone()
    if ts_ms is not None:
        ph_ts = ts_ms
    try:
        attachments = json.loads(ph_attachments) if ph_attachments else []
    except ValueError, TypeError:
        attachments = []
    if not isinstance(attachments, list):
        attachments = []

    def _attachment_json(k: int) -> str | None:
        return json.dumps([attachments[k]], ensure_ascii=False) if k < len(attachments) else None

    created = False
    for k, element_ext_id in enumerate(album.ext_ids):
        if k == 0:
            continue
        media_row = conn.execute(
            _sql(
                schema, "SELECT id, account_id, sha256 FROM {schema}.media WHERE message_id = ? AND idx = ?"
            ),
            (placeholder_id, k),
        ).fetchone()
        existing = conn.execute(
            _sql(schema, "SELECT id FROM {schema}.message WHERE channel_id = ? AND ext_id = ?"),
            (channel_id, element_ext_id),
        ).fetchone()
        if existing is not None:
            target_id = existing[0]
            if media_row is not None:
                taken = conn.execute(
                    _sql(schema, "SELECT 1 FROM {schema}.media WHERE message_id = ? AND idx = 0"),
                    (target_id,),
                ).fetchone()
                if taken is None and not _placeholder_hidden(conn, schema, target_id):
                    conn.execute(
                        _sql(schema, "UPDATE {schema}.media SET message_id = ?, idx = 0 WHERE id = ?"),
                        (target_id, media_row[0]),
                    )
                    _bump_seq_by_message_id(conn, schema, target_id)
                else:
                    conn.execute(_sql(schema, "DELETE FROM {schema}.media WHERE id = ?"), (media_row[0],))
                    media_mod.release_file(conn, schema, media_row[1], media_row[2])
            continue
        seq = next_seq(conn)
        cur = conn.execute(
            _sql(
                schema,
                "INSERT INTO {schema}.message (seq, first_seq, ext_sort, account_id, channel_id, ext_id, "
                "thread_ext_id, reply_to_ext_id, grouped_id, is_me, kind, text, attachments_json, ts, "
                "outbox_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'file', '', ?, ?, NULL)",
            ),
            (
                seq,
                seq,
                _ext_sort(element_ext_id),
                ph_account,
                channel_id,
                element_ext_id,
                ph_thread,
                ph_reply,
                album.grouped_id,
                _attachment_json(k),
                ph_ts,
            ),
        )
        created = True
        if media_row is not None:
            conn.execute(
                _sql(schema, "UPDATE {schema}.media SET message_id = ?, idx = 0 WHERE id = ?"),
                (cur.lastrowid, media_row[0]),
            )

    # Страховка: строк `media` заготовки с `idx` вне альбома не бывает (N = число файлов), но осиротевших
    # не оставляем.
    for media_id, media_account_id, sha256 in conn.execute(
        _sql(schema, "SELECT id, account_id, sha256 FROM {schema}.media WHERE message_id = ? AND idx <> 0"),
        (placeholder_id,),
    ).fetchall():
        conn.execute(_sql(schema, "DELETE FROM {schema}.media WHERE id = ?"), (media_id,))
        media_mod.release_file(conn, schema, media_account_id, sha256)

    conn.execute(
        _sql(schema, "UPDATE {schema}.message SET attachments_json = ?, grouped_id = ? WHERE id = ?"),
        (_attachment_json(0) or ph_attachments, album.grouped_id, placeholder_id),
    )
    if element0_id != placeholder_id:
        conn.execute(
            _sql(schema, "UPDATE {schema}.message SET grouped_id = ? WHERE id = ?"),
            (album.grouped_id, element0_id),
        )
    if created and ph_thread is not None and threads_mod.is_reply(ph_thread, album.ext_ids[-1]):
        threads_mod.touch_and_bump(conn, schema, channel_id, ph_thread, now_ms)
    _audit(
        conn,
        schema,
        action="outbox_album",
        outbox_id=outbox_id,
        channel_id=channel_id,
        api_token_id=_outbox_api_token(conn, schema, outbox_id),
        result="sent",
        now_ms=now_ms,
        detail={"files": len(album.ext_ids)},
    )


# Ф40-1 A: потолок времени источника из отчёта — как у прочих целых из JSON (точны до 2^53).
_REPORT_TS_MAX = 2**53


def _report_ts_ms(ext_id: str, ts_ms: object) -> int | None:
    """Время своей строки по часам ИСТОЧНИКА при отчёте `sent`. `ext_id` вида ts Slack — время из него
    (раздел A спеки 2b; единственная функция перевода — `extsort.slack_ts_to_ms`), даже если отчёт принёс и
    `ts_ms`. Иначе — `ts_ms` отчёта (Ф40-1 A: Mattermost, `create_at` поста), если это целое в разумных
    пределах; негодное значение игнорируется — отчёт из-за него не отклоняется. `None` — время строки не
    трогать (остаются часы хаба со времени создания черновика)."""
    if extsort.is_slack_ts(ext_id):
        return extsort.slack_ts_to_ms(ext_id)
    if isinstance(ts_ms, int) and not isinstance(ts_ms, bool) and 0 < ts_ms < _REPORT_TS_MAX:
        return ts_ms
    return None


def _report_sent(
    conn, schema: str, outbox_id: int, placeholder_id: int, channel_id: int, ext_id: str, now_ms: int,
    album: Album | None = None, ts_ms: object = None,
) -> None:  # fmt: skip
    other = conn.execute(
        _sql(schema, "SELECT id FROM {schema}.message WHERE channel_id = ? AND ext_id = ? AND id <> ?"),
        (channel_id, ext_id, placeholder_id),
    ).fetchone()

    conn.execute(
        _sql(
            schema,
            "UPDATE {schema}.outbox SET status = 'sent', ext_id = ?, sent_ts = ?, lease_owner = NULL, "
            "lease_expires_ts = NULL, reconcile = 0 WHERE id = ?",
        ),
        (ext_id, now_ms, outbox_id),
    )

    if album is not None:
        # П3: раскладка — ДО `_move_placeholder_media` ветки «эхо элемента 0 уже пришло».
        _apply_album(
            conn,
            schema,
            outbox_id=outbox_id,
            placeholder_id=placeholder_id,
            element0_id=other[0] if other is not None else placeholder_id,
            channel_id=channel_id,
            album=album,
            now_ms=now_ms,
            # Время из `ext_id` Slack элементам альбома не раздаётся (как было): альбомов у Slack нет.
            ts_ms=None if extsort.is_slack_ts(ext_id) else _report_ts_ms(ext_id, ts_ms),
        )

    if other is not None:
        # Эхо не склеилось раньше (другой текст/формат): настоящая строка получает outbox_id, заготовка
        # гаснет (deleted=1).
        real_id = other[0]
        conn.execute(
            _sql(schema, "UPDATE {schema}.message SET outbox_id = NULL WHERE id = ?"), (placeholder_id,)
        )
        conn.execute(
            _sql(schema, "UPDATE {schema}.message SET outbox_id = ?, seq = ? WHERE id = ?"),
            (outbox_id, next_seq(conn), real_id),
        )
        _move_placeholder_media(conn, schema, placeholder_id, real_id)
        _bump_seq_by_message_id(conn, schema, placeholder_id, deleted=True)
        return

    thread_ext_id = conn.execute(
        _sql(schema, "SELECT thread_ext_id FROM {schema}.message WHERE id = ?"), (placeholder_id,)
    ).fetchone()[0]

    # Раздел A спеки 2b: `ext_id` вида ts Slack -> `ts` заготовки ставится ИЗ НЕГО (сейчас там часы хаба со
    # времени enqueue) — единственная функция перевода, `extsort.slack_ts_to_ms`. Ф40-1 A: у прочих источников
    # — время из отчёта (`ts_ms`), если коннектор его дал.
    new_ts = _report_ts_ms(ext_id, ts_ms)
    effective_ts = new_ts if new_ts is not None else now_ms

    if thread_ext_id is not None and threads_mod.is_reply(thread_ext_id, ext_id):
        # Ф28-1: мой ответ из хаба (отчёт раньше эха) — новое свидетельство подписки, сбрасывает устаревший
        # снимок Slack «не подписан» до проверки `last_msg_ts` и `touch()` ниже.
        threads_mod.drop_stale_source_sub(conn, schema, channel_id, thread_ext_id, evidence_ext_id=ext_id)

    seq = next_seq(conn)
    if new_ts is not None:
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.message SET ext_id = ?, ext_sort = ?, ts = ?, seq = ? WHERE id = ?",
            ),
            (ext_id, _ext_sort(ext_id), new_ts, seq, placeholder_id),
        )
    else:
        conn.execute(
            _sql(schema, "UPDATE {schema}.message SET ext_id = ?, ext_sort = ?, seq = ? WHERE id = ?"),
            (ext_id, _ext_sort(ext_id), seq, placeholder_id),
        )

    eligible = threads_mod.last_msg_ts_eligible_for_new_message(
        conn,
        schema,
        channel_id,
        thread_ext_id=thread_ext_id,
        ext_id=ext_id,
        thread_broadcast=False,
        is_me=True,
        mentions_me=False,
    )
    channel_seq = next_seq(conn)
    if eligible:
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.channel SET my_msg_count = my_msg_count + 1, "
                "last_msg_ts = MAX(COALESCE(last_msg_ts, ?), ?), seq = ? WHERE id = ?",
            ),
            (effective_ts, effective_ts, channel_seq, channel_id),
        )
    else:
        conn.execute(
            _sql(schema, "UPDATE {schema}.channel SET my_msg_count = my_msg_count + 1, seq = ? WHERE id = ?"),
            (channel_seq, channel_id),
        )

    if thread_ext_id is not None and threads_mod.is_reply(thread_ext_id, ext_id):
        threads_mod.touch_and_bump(conn, schema, channel_id, thread_ext_id, now_ms)
    else:
        # Ф14-7 A1: отчёт об отправке из хаба пришёл раньше эха (эхо потом — уже не вставка) — моё сообщение
        # основной ленты лички читает ответы в тредах до себя, как вставка в `ingest`.
        msg_ts, msg_sort = conn.execute(
            _sql(schema, "SELECT ts, ext_sort FROM {schema}.message WHERE id = ?"), (placeholder_id,)
        ).fetchone()
        threads_mod.read_personal_threads_up_to(
            conn, schema, channel_id, ts=msg_ts, ext_sort=msg_sort, ext_id=ext_id
        )


def report(
    conn,
    *,
    outbox_id: int,
    owner: str,
    lease_gen: int,
    outcome: str,
    ext_id: str | None = None,
    error: str | None = None,
    retry_after_ms: int | None = None,
    now_ms: int,
    peer_ext_id: str | None = None,
    peer_title: str | None = None,
    peer_bot: bool = False,
    ext_ids: object = None,
    grouped_id: object = None,
    postponed: bool = False,
    ts_ms: object = None,
) -> None:
    """Ф40-1 A: `ts_ms` — время сообщения по часам источника (мс), только у `sent`: строка-заготовка получает
    его вместо часов хаба (`_report_ts_ms`); негодное значение игнорируется, отчёт не отклоняется. Эхо,
    пришедшее раньше отчёта, уже поставило своё время — такой отчёт строку не трогает.

    Ревью Ф34-3: `postponed` — только у `temporary` с `retry_after_ms` (иначе признак игнорируется):
    коннектор отложил строку ДО любого запроса к источнику (лимит и пауза поиска по номеру телефона). Строка
    ждёт `retry_after_ms`, попытка не тратится, аккаунт не останавливается (`flood_until` не трогается);
    если других попыток у строки не было — она снова «не начинала отправку» (отмена даёт `cancelled`).

    Ф12-1 A4: `peer_ext_id`/`peer_title` — только у `sent` по черновику беседы-заготовки этого аккаунта:
    привязка к настоящей беседе (решение 3, П3, П4) в этой же транзакции. Иначе поля игнорируются с записью в
    аудит; сам отчёт из-за них не отклоняется никогда (иначе потерялось бы «отправлено»). Ф63-2 П2:
    `peer_bot` — адресат заготовки бот: беседа получает `attrs.tg_type = "bot"` (те же условия).

    Ф11-3b A5: `ext_ids`/`grouped_id` — альбом Telegram (и `fake`): годные раскладывают заготовку с N
    вложениями на N строк (`_apply_album`); негодные игнорируются с аудитом `outbox_album_ignored`, отчёт
    принимается и заготовка остаётся одной строкой."""
    schema = db_of(outbox_id)
    row = conn.execute(
        _sql(
            schema,
            "SELECT status, lease_owner, reconcile, account_id, channel_id, api_token_id, attempts, ext_id, "
            "lease_gen "
            "FROM {schema}.outbox WHERE id = ?",
        ),
        (outbox_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"unknown outbox_id {outbox_id}")
    (status, lease_owner, reconcile, account_id, channel_id, api_token_id, attempts, current_ext_id,
     current_gen) = row  # fmt: skip

    # owner — стабильное имя коннектора, рестарт его не меняет: старый лиз от нового отличает поколение.
    same_lease = lease_owner == owner and current_gen == lease_gen
    # «Отправлено» не бывает устаревшей новостью: попытки исчерпались, lease() увёл строку в unknown, а
    # владелец того же поколения всё-таки донёс ext_id — принимаем, иначе человек нажмёт retry и отправит
    # второй раз.
    late_sent = status == "unknown" and outcome == "sent" and same_lease
    if not ((status == "sending" and same_lease) or late_sent):
        if status == "sent" and ext_id is not None and current_ext_id == ext_id:
            # эхо пришло раньше результата отправки — тихий успех, состояние то же; в аудит — для разборов
            _audit(
                conn,
                schema,
                action="outbox_report_dup",
                outbox_id=outbox_id,
                channel_id=channel_id,
                api_token_id=api_token_id,
                result="sent",
                now_ms=now_ms,
            )
            return
        raise StaleLease(f"outbox {outbox_id}: status={status!r} lease_owner={lease_owner!r} owner={owner!r}")

    if reconcile and outcome not in ("sent", "unknown"):
        raise InvalidTransition(f"reconcile-лиз принимает только sent|unknown, получено {outcome!r}")

    placeholder_id = _placeholder_id(conn, schema, outbox_id)

    if peer_ext_id is not None or peer_title is not None:
        is_draft = draft_channels.is_draft_channel(conn, schema, channel_id)
        usable = (
            outcome == "sent"
            and is_draft
            and (peer_ext_id is None or draft_channels.valid_peer_ext_id(peer_ext_id))
        )
        if not usable:
            _audit(
                conn,
                schema,
                action="outbox_peer_ignored",
                outbox_id=outbox_id,
                channel_id=channel_id,
                api_token_id=api_token_id,
                result=outcome,
                now_ms=now_ms,
                detail={"draft": is_draft, "valid": draft_channels.valid_peer_ext_id(peer_ext_id)},
            )
            peer_ext_id = peer_title = None
            peer_bot = False

    album = None
    if ext_ids is not None or grouped_id is not None:
        album, reason = _album_from_report(
            conn, schema, outbox_id=outbox_id, account_id=account_id, outcome=outcome, ext_id=ext_id,
            ext_ids=ext_ids, grouped_id=grouped_id,
        )  # fmt: skip
        if album is None:
            _audit(
                conn,
                schema,
                action="outbox_album_ignored",
                outbox_id=outbox_id,
                channel_id=channel_id,
                api_token_id=api_token_id,
                result=outcome,
                now_ms=now_ms,
                detail={
                    "reason": reason,
                    "ext_ids": len(ext_ids) if isinstance(ext_ids, list) else None,
                },
            )

    if outcome == "sent":
        if not ext_id:
            raise OutboxError("outcome='sent' требует ext_id")
        # Ф12-1 A4: черновик беседы-заготовки переезжает в настоящую беседу ДО записи `ext_id` —
        # `_report_sent` и проверка конфликта работают уже в ней.
        channel_id = draft_channels.bind_after_sent(
            conn,
            schema,
            outbox_id=outbox_id,
            channel_id=channel_id,
            ext_id=ext_id,
            peer_ext_id=peer_ext_id,
            peer_title=peer_title,
            now_ms=now_ms,
            peer_bot=peer_bot,
        )
        conflict_outbox_id = _conflicting_outbox_id(
            conn, schema, channel_id, placeholder_id, ext_id, outbox_id
        )
        if conflict_outbox_id is not None:
            # Находка F1(в): (channel_id, ext_id) уже занят ДРУГИМ outbox — не перевешиваем чужую строку
            # ленты на себя. Наш outbox -> unknown (решает человек), чужой не трогаем вовсе.
            conn.execute(
                _sql(
                    schema,
                    "UPDATE {schema}.outbox SET status = 'unknown', lease_owner = NULL, "
                    "lease_expires_ts = NULL WHERE id = ?",
                ),
                (outbox_id,),
            )
            new_status = "unknown"
            _bump_seq_by_message_id(conn, schema, placeholder_id)
            _audit(
                conn,
                schema,
                action="outbox_ext_id_conflict",
                outbox_id=outbox_id,
                channel_id=channel_id,
                api_token_id=api_token_id,
                result="unknown",
                now_ms=now_ms,
                detail={"outbox_id": outbox_id, "conflict_outbox_id": conflict_outbox_id, "ext_id": ext_id},
            )
        else:
            _report_sent(conn, schema, outbox_id, placeholder_id, channel_id, ext_id, now_ms, album, ts_ms)
            new_status = "sent"

    elif peer_flood_mod.is_peer_flood_report(outcome, error):
        # Ф69-1 B + П3: PeerFlood (новый коннектор — `permanent`, старый — `temporary`) — строка `failed` и
        # больше не повторяется сама (петля 16.10); стоп — только новых людей или весь аккаунт, в этой же
        # транзакции и ДО `draft_channels.settle` (заготовка уходит в архив).
        scope = peer_flood_mod.apply_report(
            conn, schema, account_id=account_id, channel_id=channel_id, now_ms=now_ms
        )
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.outbox SET status = 'failed', error = ?, lease_owner = NULL, "
                "lease_expires_ts = NULL WHERE id = ?",
            ),
            (peer_flood_mod.FAILED_TEXT, outbox_id),
        )
        new_status = "failed"
        _bump_seq_by_message_id(conn, schema, placeholder_id)
        _audit(
            conn,
            schema,
            action="peer_flood",
            outbox_id=outbox_id,
            channel_id=channel_id,
            api_token_id=api_token_id,
            result=scope,
            now_ms=now_ms,
            detail={"account_id": account_id, "outcome": outcome},
        )

    elif outcome == "temporary" and postponed and retry_after_ms is not None:
        # Все прошлые лизы строки кончились отчётом с возвратом попытки (`flood`, `postponed` — источник
        # сообщение не принимал) — счётчик после возврата 0: отправка не начиналась, `send_started_ts`
        # снимается.
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.outbox SET status = 'queued', not_before_ts = ?, lease_owner = NULL, "
                "lease_expires_ts = NULL, attempts = MAX(attempts - 1, 0), "
                "send_started_ts = CASE WHEN attempts <= 1 THEN NULL ELSE send_started_ts END WHERE id = ?",
            ),
            (now_ms + retry_after_ms, outbox_id),
        )
        new_status = "queued"
        _bump_seq_by_message_id(conn, schema, placeholder_id)

    elif outcome == "temporary":
        # Порог >= (не >), в отличие от lease()'ой стейл-ветки: "после 5 попыток -> failed" (спека, тест 12)
        # означает, что 5-й провал уже приводит к failed, а не требует 6-й попытки.
        if attempts >= _MAX_ATTEMPTS:
            conn.execute(
                _sql(
                    schema,
                    "UPDATE {schema}.outbox SET status = 'failed', error = ?, lease_owner = NULL, "
                    "lease_expires_ts = NULL WHERE id = ?",
                ),
                (error or "temporary: попытки исчерпаны", outbox_id),
            )
            new_status = "failed"
        else:
            backoff = (
                retry_after_ms
                if retry_after_ms is not None
                else min(_BACKOFF_BASE_MS * 2 ** (attempts - 1), _BACKOFF_CAP_MS)
            )
            conn.execute(
                _sql(
                    schema,
                    "UPDATE {schema}.outbox SET status = 'queued', not_before_ts = ?, lease_owner = NULL, "
                    "lease_expires_ts = NULL WHERE id = ?",
                ),
                (now_ms + backoff, outbox_id),
            )
            new_status = "queued"
        _bump_seq_by_message_id(conn, schema, placeholder_id)

    elif outcome == "flood":
        if retry_after_ms is None:
            raise OutboxError("outcome='flood' требует retry_after_ms")
        conn.execute(
            "UPDATE main.account SET flood_until = ? WHERE id = ?", (now_ms + retry_after_ms, account_id)
        )
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.outbox SET status = 'queued', not_before_ts = ?, lease_owner = NULL, "
                "lease_expires_ts = NULL, attempts = MAX(attempts - 1, 0) WHERE id = ?",
            ),
            (now_ms + retry_after_ms, outbox_id),
        )
        new_status = "queued"
        _bump_seq_by_message_id(conn, schema, placeholder_id)

    elif outcome == "permanent":
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.outbox SET status = 'failed', error = ?, lease_owner = NULL, "
                "lease_expires_ts = NULL WHERE id = ?",
            ),
            (error, outbox_id),
        )
        new_status = "failed"
        _bump_seq_by_message_id(conn, schema, placeholder_id)

    elif outcome == "unknown":
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.outbox SET status = 'unknown', lease_owner = NULL, lease_expires_ts = NULL "
                "WHERE id = ?",
            ),
            (outbox_id,),
        )
        new_status = "unknown"
        _bump_seq_by_message_id(conn, schema, placeholder_id)

    else:
        raise OutboxError(f"unknown outcome {outcome!r}")

    if new_status == "failed":
        draft_channels.settle(conn, schema, channel_id, status=new_status, outbox_id=outbox_id, now_ms=now_ms)
    _clear_text_if_hidden(conn, schema, outbox_id, placeholder_id)
    _audit(
        conn,
        schema,
        action="outbox_report",
        outbox_id=outbox_id,
        channel_id=channel_id,
        api_token_id=api_token_id,
        result=new_status,
        now_ms=now_ms,
    )


def hold_after_restore(conn, *, now_ms: int) -> int:
    """Ф33-3 (К12): после восстановления из копии — до первого старта ядра (`hub backup --hold-outbox`) —
    каждая строка `queued`/`sending` обоих файлов становится `unknown` («могло уйти»): прежняя машина могла
    отправить её уже после снимка, и повтор ушёл бы человеку вторым сообщением. Решает владелец теми же
    кнопками, что у любого `unknown` («Отправлено» / «Не отправлено» / «Повторить»; повтор — сразу, без
    прежнего срока). `maybe_sent` ставится: при «Повторить» коннектор сперва ищет своё сообщение в истории
    (у строк с файлом «Повторить» его сбрасывает, как всегда). `unknown`, `pending_approval` и конечные
    статусы не трогаются. Возвращает число удержанных строк.

    Финальное ревью Ф33 (17.10): `queued` со сроком `send_at_ts` позже `now_ms` (момента восстановления) не
    трогается — прежняя машина отправить её не могла (срок не наступил, а она с тех пор мертва или выключена);
    догон уйдёт в свой срок со своими проверками (`cancel_if_reply`). `sending` — всегда «могло уйти»."""
    held = 0
    for schema in ("main", "work"):
        rows = conn.execute(
            _sql(
                schema,
                "SELECT id, channel_id, status, api_token_id FROM {schema}.outbox "
                "WHERE (status = 'sending' "
                "OR (status = 'queued' AND (send_at_ts IS NULL OR send_at_ts <= ?))) ORDER BY id",
            ),
            (now_ms,),
        ).fetchall()
        for outbox_id, channel_id, status, api_token_id in rows:
            conn.execute(
                _sql(
                    schema,
                    "UPDATE {schema}.outbox SET status = 'unknown', error = ?, maybe_sent = 1, "
                    "reconcile = 0, lease_owner = NULL, lease_expires_ts = NULL WHERE id = ?",
                ),
                (RESTORE_HOLD_ERROR, outbox_id),
            )
            placeholder = conn.execute(
                _sql(schema, "SELECT id FROM {schema}.message WHERE outbox_id = ?"), (outbox_id,)
            ).fetchone()
            if placeholder is not None:
                _bump_seq_by_message_id(conn, schema, placeholder[0])
            _audit(
                conn,
                schema,
                action="outbox_restore_hold",
                outbox_id=outbox_id,
                channel_id=channel_id,
                api_token_id=api_token_id,
                result="unknown",
                now_ms=now_ms,
                detail={"from": status},
            )
            held += 1
    return held


def resolve_unknown(conn, outbox_id: int, *, decision: str, now_ms: int) -> str:
    """Вернёт итоговый статус — единственный источник правды о переходах, API его только передаёт."""
    schema = db_of(outbox_id)
    row = conn.execute(
        _sql(
            schema,
            "SELECT status, channel_id, api_token_id, text, file_json FROM {schema}.outbox WHERE id = ?",
        ),
        (outbox_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"unknown outbox_id {outbox_id}")
    status, channel_id, api_token_id, current_text, file_json = row
    if status != "unknown":
        raise InvalidTransition(f"resolve_unknown: status={status!r}, ожидался unknown")

    # Поправки после opus-критика кода (R7): `retry` заготовки, чей текст уже стёрла ретенция (пусто), снова
    # ушёл бы в очередь и коннектор отправил бы пустое сообщение — у `resolve_unknown` нет параметра нового
    # текста (в отличие от `approve`), поэтому единственный честный исход здесь — отказ.
    # Ф10-4 П4: «текст стёрт» — пустой текст БЕЗ файла; файл без подписи (текст пуст по замыслу) повторяется.
    has_files = _parse_file_json(file_json) is not None
    if decision == "retry" and current_text == "" and not has_files:
        raise InvalidTransition("resolve_unknown(retry): текст стёрт по сроку хранения")

    placeholder_id = _placeholder_id(conn, schema, outbox_id)

    if decision == "sent":
        conn.execute(
            _sql(schema, "UPDATE {schema}.outbox SET status = 'sent', sent_ts = ? WHERE id = ?"),
            (now_ms, outbox_id),
        )
        # Ни ext_id, ни эхо у нас нет: my_msg_count раньше не рос, человек подтвердил отправку вручную —
        # считаем.
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.channel SET my_msg_count = my_msg_count + 1, "
                "last_msg_ts = MAX(COALESCE(last_msg_ts, ?), ?), seq = ? WHERE id = ?",
            ),
            (now_ms, now_ms, next_seq(conn), channel_id),
        )
        new_status = "sent"
    elif decision == "not_sent":
        conn.execute(_sql(schema, "UPDATE {schema}.outbox SET status = 'failed' WHERE id = ?"), (outbox_id,))
        new_status = "failed"
    elif decision == "retry":
        # Приёмка Ф10-4 (ревью): у строки с файлом «Повторить» — решение владельца, что файл НЕ дошёл;
        # `maybe_sent` сбрасывается, иначе Slack только ищет своё сообщение и снова отвечает `unknown` без
        # заливки. У текста — как было (поиск в истории перед отправкой остаётся).
        # Ф30-1 П2: «Повторить» — решение владельца: `send_at_ts` обнуляется, иначе `lease`
        # снова увидел бы опоздание и вернул строку в `unknown`.
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.outbox SET status = 'queued', attempts = 0, reconcile = 0, "
                "send_at_ts = NULL, not_before_ts = ?, lease_owner = NULL, lease_expires_ts = NULL, "
                "maybe_sent = CASE WHEN ? THEN 0 ELSE maybe_sent END WHERE id = ?",
            ),
            (now_ms, 1 if has_files else 0, outbox_id),
        )
        new_status = "queued"
    else:
        raise OutboxError(f"unknown decision {decision!r}")

    _bump_seq_by_message_id(conn, schema, placeholder_id)
    # Ф12-1 П2: «не ушло» — беседа-заготовка в архив; «ушло» без `ext_id` — в архив, но кандидат привязки
    # (П4).
    draft_channels.settle(conn, schema, channel_id, status=new_status, outbox_id=outbox_id, now_ms=now_ms)
    _clear_text_if_hidden(conn, schema, outbox_id, placeholder_id)
    _audit(
        conn,
        schema,
        action="outbox_resolve_unknown",
        outbox_id=outbox_id,
        channel_id=channel_id,
        api_token_id=api_token_id,
        result=new_status,
        now_ms=now_ms,
    )
    return new_status


def match_echo(conn, schema: str, channel_id: int, ev) -> tuple[int, int] | None:
    """Заготовка (outbox_id, message_id), которой принадлежит это `is_me`-эхо, либо None.

    См. спеку, раздел «Склейка эха».
    """
    send_token = getattr(ev, "send_token", None)
    if send_token:
        # Токен доказывает, что отправка состоялась, в каком бы статусе ни была строка: коннектор мог
        # отчитаться temporary (строка снова queued) или permanent, а сообщение ушло. Не склеить здесь =
        # отправить второй раз. Эвристика по тексту такой силы не имеет и queued/failed не трогает.
        row = conn.execute(
            _sql(
                schema,
                "SELECT o.id, m.id FROM {schema}.outbox o JOIN {schema}.message m ON m.outbox_id = o.id "
                "WHERE o.channel_id = ? AND o.send_token = ? AND o.status <> 'cancelled' "
                "AND o.send_started_ts IS NOT NULL AND m.ext_id IS NULL AND m.deleted = 0",
            ),
            (channel_id, send_token),
        ).fetchone()
        return (row[0], row[1]) if row is not None else None

    # Ревью Ф8-4: у скрытой заготовки текст стёрт — её сравниваем по хешу текста строки очереди. Условия те
    # же, что у видимой (свой канал, не отменена, уже отправлялась, без ext_id, окно 60 с ниже), а хеш — того
    # же текста, что раньше сравнивался напрямую: чужое сообщение склеится не шире, чем склеивалось по тексту.
    candidates = conn.execute(
        _sql(
            schema,
            "SELECT o.id, m.id, o.send_started_ts, o.file_json, a.source FROM {schema}.outbox o "
            "JOIN {schema}.message m ON m.outbox_id = o.id LEFT JOIN main.account a ON a.id = o.account_id "
            "WHERE o.channel_id = ? AND o.status <> 'cancelled' "
            "AND o.send_started_ts IS NOT NULL AND m.ext_id IS NULL AND m.deleted = 0 "
            "AND (m.text = ? OR (m.hidden = 1 AND o.text_sha256 = ?)) "
            # Ф10-4 П6: файл без подписи по тексту не склеивается — пустой текст совпал бы с любым своим
            # фото с телефона в окне. Его строку находит `ext_id` отчёта коннектора.
            "AND (o.file_json IS NULL OR ? <> '') "
            "ORDER BY o.id",
        ),
        (channel_id, ev.text, text_sha256(ev.text) if ev.text is not None else None, ev.text or ""),
    ).fetchall()
    for outbox_id, message_id, send_started_ts, file_json, source in candidates:
        if source == "telegram" and len(_parse_file_json(file_json) or []) >= 2:
            # Ф11-3b решение 4/A6: альбом Telegram по тексту не склеивается никогда — сверка приносит его
            # элементы по одному, подпись совпала бы только у первого, остальные вложения заготовки осиротели
            # бы. Его закрывает только отчёт коннектора (`ext_ids`) или решение владельца.
            continue
        if send_started_ts is not None and abs(ev.ts - send_started_ts) <= _ECHO_WINDOW_MS:
            return outbox_id, message_id
    return None


def apply_echo(
    conn,
    schema: str,
    channel_id: int,
    outbox_id: int,
    message_id: int,
    *,
    ext_id: str,
    ts: int,
    content: dict,
    now_ms: int,
    purged: int = 0,
) -> int:
    """Слить эхо с заготовкой: применить содержимое события к строке-заготовке и перевести outbox в sent.

    Ф8-4 П1: заготовка удалена владельцем у себя в хабе — содержимое эха идёт через ту же функцию очистки, что у
    скрытия (`retention.clean_message_content`), `purged = 1`; `hidden` эхо не снимает никогда."""
    if _placeholder_hidden(conn, schema, message_id):
        content = dict(content)
        retention.clean_message_content(content)
        purged = 1
    seq = next_seq(conn)
    columns = ["ext_id", "ext_sort", "ts", "seq", "purged", *content.keys()]
    assignments = ", ".join(f"{col} = ?" for col in columns)
    conn.execute(
        _sql(schema, "UPDATE {schema}.message SET {set_clause} WHERE id = ?", set_clause=assignments),
        (ext_id, _ext_sort(ext_id), ts, seq, purged, *content.values(), message_id),
    )

    old_status, api_token_id = conn.execute(
        _sql(schema, "SELECT status, api_token_id FROM {schema}.outbox WHERE id = ?"), (outbox_id,)
    ).fetchone()

    if old_status != "sent":
        conn.execute(
            _sql(
                schema,
                "UPDATE {schema}.outbox SET status = 'sent', ext_id = ?, sent_ts = ?, lease_owner = NULL, "
                "lease_expires_ts = NULL, reconcile = 0 WHERE id = ?",
            ),
            (ext_id, now_ms, outbox_id),
        )
        eligible = threads_mod.last_msg_ts_eligible_for_new_message(
            conn,
            schema,
            channel_id,
            thread_ext_id=content.get("thread_ext_id"),
            ext_id=ext_id,
            thread_broadcast=bool(content.get("thread_broadcast")),
            is_me=bool(content.get("is_me")),
            mentions_me=bool(content.get("mentions_me")),
        )
        channel_seq = next_seq(conn)
        if eligible:
            conn.execute(
                _sql(
                    schema,
                    "UPDATE {schema}.channel SET my_msg_count = my_msg_count + 1, "
                    "last_msg_ts = MAX(COALESCE(last_msg_ts, ?), ?), seq = ? WHERE id = ?",
                ),
                (ts, ts, channel_seq, channel_id),
            )
        else:
            conn.execute(
                _sql(
                    schema,
                    "UPDATE {schema}.channel SET my_msg_count = my_msg_count + 1, seq = ? WHERE id = ?",
                ),
                (channel_seq, channel_id),
            )
        seq = max(seq, channel_seq)
    else:
        conn.execute(_sql(schema, "UPDATE {schema}.outbox SET ext_id = ? WHERE id = ?"), (ext_id, outbox_id))

    _clear_text_if_hidden(conn, schema, outbox_id, message_id)
    _audit(
        conn,
        schema,
        action="outbox_echo",
        outbox_id=outbox_id,
        channel_id=channel_id,
        api_token_id=api_token_id,
        result="sent",
        now_ms=now_ms,
    )
    return seq
