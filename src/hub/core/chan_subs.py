"""Подписка сессии Claude Code на беседу и сигналы о новых входящих (Ф14-5, docs/architecture.md). Образец — `subs.py`
(подписка на проект, Ф13-1); общие части (живость процесса,
имя сессии, 410, строка подписки) берутся оттуда, не копируются.

Модуль пишет только внутри `Db.write()`, время — параметром `now_ms`. Текстов нет: сигнал — только числа
(`channel_id`, `account_id`, `msg_count`, id последнего сообщения). Сигнал хранит id сообщений, не `seq` (П2).

Здесь же сигналы сообщений подписке на ПРОЕКТ в режиме `messages` (П20): та же таблица `session_chan_signal`,
строка с `project_sub_id`; выдача — прежним ожиданием 13-1 (`subs.deliver` -> `deliver_project_messages`).

Импорт `queries` здесь нельзя (`queries` -> `ingest` -> этот модуль): видимость беседы токену — по
`principal.project_ids` (как `queries._scope_predicate`).
"""

import json
import re
import uuid
from typing import NamedTuple

from . import agent_items as agent_items_mod
from . import engine as engine_mod
from . import held as held_mod
from . import keywords as keywords_mod
from . import machines as machines_mod
from . import session_host as session_host_mod
from . import sub_accounts, subs, transcripts
from . import threads as threads_mod
from . import unsorted as unsorted_mod
from .audit import write_audit
from .db import db_of

# Ф35-0 (план 11.40): было 20; владелец 16.10: мало, сессия пропустила заказ — 500. На подписку
# один `pending`, выдача — до `MAX_SIGNALS_PER_DELIVERY` (остальные — следующей выдачей, старшие первыми).
MAX_CHAN_SUBS = 500
CHAN_DEBOUNCE_MS = subs.CHAN_DEBOUNCE_MS
# П12/П19: сообщение старше подписки не сигналит; запас 10 мин — у почты `ts` — дата из заголовка отправителя.
SIGNAL_TS_SLACK_MS = 10 * 60 * 1000
MAX_SIGNALS_PER_DELIVERY = 20
RULES_MAX_IN_SIGNAL = 5  # Ф63-1 П4: номеров правил входящих в одном сигнале
# Ф20-1 D/П10: категория сигнала сообщений — сильнейшая из накопленных. `NULL` у старых строк — «сообщение».
# Ф25-1 A4: `keyword` — чужое сообщение с ключом подписки «лично мне» (слабее личного, сильнее прочего).
CAT_RANK = {"other": 0, "keyword": 1, "thread": 2, "mention": 3, "dm": 4}
CATS = tuple(CAT_RANK)
# Ф67-1: категории, которые копятся в пачку подписки (`batch_min`); личные (`dm`, `mention`, `thread`) — сразу
# и забирают накопленное.
BATCH_CATS = ("other", "keyword")
# 21.10: личные категории — для сессии хаба срочны (`valid_state` -> `held_signals.Waiting.urgent`): подаются
# посреди хода сразу и поднимают «тяжёлую холодную».
PERSONAL_CATS = ("dm", "mention", "thread")
# Родитель треда в сигнале — только формат `ts` Slack или id поста Mattermost (Ф40-1 C: 26 символов
# `[a-z0-9]`) — защита вывода фонового ожидания: текста в сигнале нет. Только `fullmatch`: `$` пропустил бы
# хвостовой `\n`.
THREAD_EXT_RE = re.compile(r"\d{1,12}\.\d{1,8}|[a-z0-9]{26}", re.ASCII)
# Ф20-1 П6: сообщение, которое фильтр подписки на проект отсеял, — строка `skipped` с этой причиной (одна на
# подписку, беседу и причину): `msg_signal` карточки по нему — «сессия получила», дежурная не будится.
# Ф25-1 A4: `no_keyword` — у подписки с ключами чужое сообщение без ключа или с минус-словом.
FILTER_REASONS = ("bot", "not_personal", "no_keyword")
# Ф31-1 решение 1: перепроверка ключей (после расшифровки голосового и настоящей правки) — только сообщения не
# старше суток; решение 2 + П8: пометки «ключ сработал» живут неделю.
RECHECK_MAX_AGE_MS = 24 * 60 * 60 * 1000
KEYWORD_HIT_KEEP_MS = 7 * 24 * 60 * 60 * 1000

END_REASONS = (
    "session_gone",
    "conversation_changed",
    "unsubscribed",
    "ended_by_owner",
    "token_revoked",
    "replaced",
    "moved",
    "channel_gone",
    "registry_unreadable",
    # Ф16-1 (3.6): владелец переключил движок сессий — подписки чужого движка сняты ядром.
    engine_mod.END_REASON,
    subs.HANDOFF_END_REASON,
    subs.TOKEN_REISSUED_END_REASON,
)
CLIENT_END_REASONS = ("unsubscribed", "conversation_changed", "registry_unreadable")
# Ф32-1 (решение 2, П7): подписка на проект с ролью возвращает подписки на беседы прежней сессии роли, только
# если и источник роли, и сама подписка на беседу кончились «сами» (сессии не стало, новый разговор). Явная
# отписка, снятие владельцем, отзыв токена, перенос и архив беседы — насовсем.
# 21.10 (владелец: при «Новой с передачей дел» слетали подписки на беседы): передача дел — тоже «сама».
# Ф92-1 (B3): и перевыпуск токена профиля (`token_reissued`; явный отзыв `token_revoked` — насовсем).
RESTORE_REASONS = (
    "session_gone",
    "conversation_changed",
    "registry_unreadable",
    engine_mod.END_REASON,
    subs.HANDOFF_END_REASON,
    subs.TOKEN_REISSUED_END_REASON,
)
RESTORE_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000  # П6: источник роли старше — беседы не возвращаются
# Ф36-1 П1: ждущие сигналы подписки на проект переезжают новой, только если источник кончился «сам» —
# те же причины плюс `replaced` (мёртвую подписку линии сменила новая).
CARRY_REASONS = (*RESTORE_REASONS, "replaced")
# Ф41-2а П7: все сессии владельца ходят одним токеном профиля; его перевыпуск завершает и подписки сессий хаба
# (`token_revoked`; успела подписаться раньше чистки — прежняя `replaced`). Когда новая подписка — ТОЙ ЖЕ
# сессии хаба (та же условная тройка), владелец тот же, сменился только токен: беседы возвращаются и ждущие
# сигналы переезжают и с этими причинами. Для внешних сессий — как было (насовсем).
SAME_HELD_REASONS = ("token_revoked", "replaced")
# Ф92-1 (B3): внешняя сессия (та же тройка pid, starttime, session_id) подписалась другим токеном — прежняя
# подписка `replaced`, владелец тот же: беседы прежнего токена переходят ей. Отзыв токена владельцем
# (`token_revoked`) у внешней сессии — по-прежнему насовсем.
SAME_EXT_REASONS = ("replaced", subs.TOKEN_REISSUED_END_REASON)
# П2(б): добор окна «источник снят -> новая подписка» — чужие входящие не старше суток, не больше 200.
RESTORE_WINDOW_MS = 24 * 60 * 60 * 1000
RESTORE_WINDOW_MAX = 200
# Ф36-1 B3: срок сигнала «по ключу» — ближайший узел сетки интервала пачки не раньше чем через минуту (спам
# успевают удалить, голосовое — расшифровать).
BATCH_MIN_HOLD_MS = 60_000
# Ф36-1 C: сообщения сигнала удалены к моменту выдачи — строка `skipped` с этой причиной. Не из фильтров
# (`FILTER_REASONS`): дежурная по такой строке считает «сессия получила».
DELETED_REASON = "deleted"
# Ф36-1 П1: ждущие сигналы завершённой подписки на проект переезжают новой подписке линии, если источник
# завершён не раньше этого срока.
CARRY_MAX_AGE_MS = 6 * 60 * 60 * 1000

_COLUMNS = (
    "id, channel_id, project_id, api_token_id, session_pid, session_proc_start, session_id, session_cwd, "
    "session_name, created_ts, last_wait_ts, last_delivery_ts, ended_ts, end_reason, keywords_json, "
    "session_host, session_engine"
)


class ChanSubRec(NamedTuple):
    id: int
    channel_id: int
    project_id: int | None
    api_token_id: int
    session_pid: int
    session_proc_start: int
    session_id: str
    session_cwd: str
    session_name: str | None
    created_ts: int
    last_wait_ts: int | None
    last_delivery_ts: int | None
    ended_ts: int | None
    end_reason: str | None
    # Ф31-1 решение 4: ключи подписки на беседу (как `session_sub.keywords_json`), NULL — каждое входящее.
    keywords_json: str | None = None
    session_host: str | None = None  # Ф33-1: машина сессии (как `subs.SubRec.session_host`)
    session_engine: str | None = None  # Ф74-2: движок сессии (как `subs.SubRec.session_engine`)


class ChanWaiters(subs.SubWaiters):
    """П8: одно ожидание на СЕССИЮ — ключ `(token_id, pid, proc_start, session_id)` (Ф16-4, П1: у OpenCode в
    одном процессе много сессий, их ожидания друг друга не вытесняют; у Claude Code на pid один разговор);
    новое вытесняет прежнее (409 `superseded`). Свой объект (не `SubWaiters` подписок на проект), общий для
    публичного и внутреннего приложений (`waiting` в `/admin/subs`). Рядом с меткой — `session_id` ожидания:
    после `/clear` висящее ожидание прежнего разговора не считается ожиданием нового (П15 + П17)."""

    def __init__(self) -> None:
        super().__init__()
        self._session_ids: dict = {}

    def enter(self, key, session_id: str | None = None) -> tuple[object, bool]:  # type: ignore[override]
        mark, had = super().enter(key)
        self._session_ids[key] = session_id
        return mark, had

    def leave(self, key, mark: object) -> None:
        if self.is_current(key, mark):
            self._session_ids.pop(key, None)
        super().leave(key, mark)

    def waiting(self, key, session_id: str | None = None) -> bool:  # type: ignore[override]
        """Идёт ли ожидание сессии; с `session_id` — только ожидание этого разговора."""
        if not super().waiting(key):
            return False
        return session_id is None or self._session_ids.get(key) == session_id


def session_key(token_id: int, pid: int, proc_start: int, session_id: str) -> tuple[int, int, int, str]:
    return (token_id, pid, proc_start, session_id)


def rec_key(rec: ChanSubRec) -> tuple[int, int, int, str]:
    return session_key(rec.api_token_id, rec.session_pid, rec.session_proc_start, rec.session_id)


# --- чтение ---------------------------------------------------------------------------------------------


def _select(conn, where: str, params: tuple) -> list[ChanSubRec]:
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM main.session_chan_sub WHERE {where} ORDER BY id",  # noqa: S608 — константы
        params,
    ).fetchall()
    return [ChanSubRec(*row) for row in rows]


def get(conn, sub_id: int) -> ChanSubRec | None:
    found = _select(conn, "id = ?", (sub_id,))
    return found[0] if found else None


def live_subs_of_channel(conn, channel_id: int) -> list[ChanSubRec]:
    """Ф39 A2 п.1: ВСЕ живые подписки беседы, по `id` (у каждой сессии своя — A0)."""
    return _select(conn, "channel_id = ? AND ended_ts IS NULL", (channel_id,))


def live_of_session(conn, channel_id: int, key: tuple[int, int, int, str]) -> ChanSubRec | None:
    """Ф39 A2 п.1: живая подписка беседы у сессии `key` (`rec_key`: токен, pid, starttime, `session_id`)."""
    found = _select(
        conn,
        "channel_id = ? AND ended_ts IS NULL AND api_token_id = ? AND session_pid = ? "
        "AND session_proc_start = ? AND session_id = ?",
        (channel_id, *key),
    )
    return found[0] if found else None


def listeners(
    conn, channel_id: int, *, except_key: tuple[int, int, int, str] | None = None
) -> list[ChanSubRec]:
    """Ф39 A2 п.10: живые подписки беседы, чей процесс жив (`subs.is_alive`), кроме сессии `except_key`."""
    return [
        rec
        for rec in live_subs_of_channel(conn, channel_id)
        if (except_key is None or rec_key(rec) != except_key) and subs.rec_session_alive(conn, rec)
    ]


def last_of_channel(conn, channel_id: int) -> list[ChanSubRec]:
    """Подписки беседы, новые первыми (снятие уже завершённой — 200 с её строкой)."""
    recs = _select(conn, "channel_id = ?", (channel_id,))
    recs.reverse()
    return recs


def live_of_process(conn, pid: int, proc_start: int, token_id: int | None = None) -> list[ChanSubRec]:
    """Живые подписки процесса (все разговоры); `token_id` — только этого токена."""
    if token_id is None:
        return _select(
            conn, "session_pid = ? AND session_proc_start = ? AND ended_ts IS NULL", (pid, proc_start)
        )
    return _select(
        conn,
        "session_pid = ? AND session_proc_start = ? AND api_token_id = ? AND ended_ts IS NULL",
        (pid, proc_start, token_id),
    )


def last_ended_of_session(
    conn, token_id: int, pid: int, proc_start: int, session_id: str
) -> ChanSubRec | None:
    """Последняя завершённая подписка этого разговора — причина для 410."""
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM main.session_chan_sub WHERE api_token_id = ? AND session_pid = ? "  # noqa: S608
        "AND session_proc_start = ? AND session_id = ? AND ended_ts IS NOT NULL "
        "ORDER BY ended_ts DESC, id DESC LIMIT 1",
        (token_id, pid, proc_start, session_id),
    ).fetchone()
    return ChanSubRec(*row) if row is not None else None


class _Channel(NamedTuple):
    project_id: int | None
    archived: int
    ext_id: str
    account_id: int


def _channel(conn, channel_id: int) -> _Channel | None:
    """Беседа подписки. Ф54-2 (П2): служебная личка с ботом хаба подпискам не существует — `None`, как
    удалённая (подписываются только сессии, владельцу подписка не нужна)."""
    from . import service_bot as service_bot_mod

    if channel_id in service_bot_mod.channel_ids(conn):
        return None
    schema = db_of(channel_id)
    row = conn.execute(
        f"SELECT project_id, archived, ext_id, account_id FROM {schema}.channel WHERE id = ?",  # noqa: S608
        (channel_id,),
    ).fetchone()
    return _Channel(*row) if row is not None else None


def visible(project_ids: frozenset[int] | None, project_id: int | None) -> bool:
    """Беседа видна токену: без списка проектов — все (и «Неразобранное»), со списком — только его проекты."""
    return project_ids is None or (project_id is not None and project_id in project_ids)


def pending_counts(conn, sub_ids: list[int]) -> dict[int, int]:
    if not sub_ids:
        return {}
    placeholders = ",".join("?" for _ in sub_ids)
    rows = conn.execute(
        f"SELECT chan_sub_id, COUNT(*) FROM main.session_chan_signal WHERE status = 'pending' "  # noqa: S608
        f"AND chan_sub_id IN ({placeholders}) GROUP BY chan_sub_id",
        sub_ids,
    ).fetchall()
    return dict(rows)


def hold_ms(conn, channel_id: int, first_message_id: int | None, *, now_ms: int) -> int:
    """Ф14-6b П1: сигнал сообщений держится, пока хоть одно его сообщение ждёт расшифровку (не дольше
    `transcripts.TRANSCRIBE_WAIT_S` от готовности медиа). `0` — не держать."""
    if first_message_id is None:
        return 0
    return transcripts.signal_hold_ms(conn, channel_id, first_message_id, now_ms=now_ms)


def batch_ready_ts(now_ms: int, batch_ms: int) -> int:
    """Ф36-1 B3: срок сигнала «по ключу» — `ceil((now_ms + BATCH_MIN_HOLD_MS) / batch_ms) * batch_ms`. Сетка —
    кратные интервала от эпохи: ожидание проекта и ожидание бесед одной сессии выходят в один момент,
    отдельного состояния «окно пачки» нет."""
    return -(-(now_ms + BATCH_MIN_HOLD_MS) // batch_ms) * batch_ms


def ready_state(
    conn, *, chan_sub_ids: list[int] | None = None, project_sub_id: int | None = None, now_ms: int
) -> tuple[int, int]:
    """Читатель, для ожиданий (Ф14-6b П1). `(готовых к выдаче pending, мс до конца удержания ближайшего
    удержанного — 0, если удержанных нет)`. Удержанные в «готовые» не входят: ожидание с одними удержанными
    спит, а не зовёт `deliver` в цикле (записи будили бы всех ожидающих). `pending_counts` (строка подписки,
    `GET /subs`) — как прежде, считает и удержанные.

    Ф36-1 B5 + П6: строка со сроком пачки в будущем (`ready_ts > now_ms`) не готова, её срок — `ready_ts -
    now_ms`; такие отсекаются ДО `hold_ms` (расшифровку по ним не спрашиваем)."""
    if chan_sub_ids is not None:
        if not chan_sub_ids:
            return 0, 0
        placeholders = ",".join("?" for _ in chan_sub_ids)
        rows = conn.execute(
            "SELECT channel_id, first_message_id, ready_ts FROM main.session_chan_signal "  # noqa: S608
            f"WHERE status = 'pending' AND chan_sub_id IN ({placeholders})",
            chan_sub_ids,
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT channel_id, first_message_id, ready_ts FROM main.session_chan_signal "
            "WHERE status = 'pending' AND project_sub_id = ?",
            (project_sub_id,),
        ).fetchall()
    ready = 0
    nearest = 0
    for channel_id, first_id, ready_ts in rows:
        if ready_ts is not None and ready_ts > now_ms:
            held = ready_ts - now_ms  # срок пачки ещё не пришёл
        else:
            held = hold_ms(conn, channel_id, first_id, now_ms=now_ms)
        if held <= 0:
            ready += 1
        elif nearest == 0 or held < nearest:
            nearest = held
    return ready, nearest


def chan_json(
    rec: ChanSubRec,
    *,
    now_ms: int,
    waiting: bool,
    pending: int,
    admin: bool = False,
    me: tuple[int, int, str] | None = None,
    role: tuple[str | None, int] | None = None,
    hub: held_mod.HubSession | None = None,
    machine: dict | None = None,
) -> dict:
    """П6: форма `subs.sub_json` плюс `channel_id`; `waiting` — по ожиданию сессии; `me` — поле `mine`
    (Ф16-4, П17). Ф39 A2 п.9: `role`, `role_no` — роль сессии подписки (`subs.session_role`); роли нет —
    оба `null`. Ф41-2а (3.6): `hub` — запись сессии хаба подписки: `held`, `hub_session_id`, `state`. Ф77-1
    (A5): `machine` — метка машины сессии."""
    result = subs.session_json(
        rec, now_ms=now_ms, waiting=waiting, pending=pending, admin=admin, me=me, machine=machine
    )
    if hub is not None:
        result.update(held_mod.sub_fields(hub))
    result["channel_id"] = rec.channel_id
    result["project_id"] = rec.project_id
    result["keywords"] = keywords_mod.public_spec(rec.keywords_json)  # Ф31-1 решение 4; Ф36-1 A4
    result["batch_min"] = keywords_mod.batch_minutes(rec.keywords_json)  # Ф67-1: действующий интервал пачки
    name, number = role if role is not None else (None, 1)
    result["role"] = name
    result["role_no"] = number if name is not None else None
    return result


def _role(conn, rec: ChanSubRec) -> tuple[str | None, int]:
    """Ф39 A2 п.9: роль сессии подписки на беседу — по НЫНЕШНЕМУ проекту беседы."""
    channel = _channel(conn, rec.channel_id)
    project_id = channel.project_id if channel is not None else rec.project_id
    return subs.session_role(conn, rec_key(rec), project_id)


def one_json(conn, rec: ChanSubRec, *, now_ms: int, waiters: ChanWaiters | None, admin: bool = False) -> dict:
    pending = pending_counts(conn, [rec.id]).get(rec.id, 0)
    waiting = waiters.waiting(rec_key(rec), rec.session_id) if waiters is not None else False
    return chan_json(
        rec,
        now_ms=now_ms,
        waiting=waiting,
        pending=pending,
        admin=admin,
        role=_role(conn, rec),
        hub=held_mod.of_rec(conn, rec),
        machine=machines_mod.of_rec(conn, rec, now_ms=now_ms),
    )


def list_chan_subs(
    conn,
    *,
    project_ids: frozenset[int] | None,
    live_only: bool,
    now_ms: int,
    waiters: ChanWaiters | None,
    admin: bool = False,
    me: tuple[int, int, str] | None = None,
    ended_ms: int = subs.ENDED_RECENT_MS,
) -> list[dict]:
    """Поле `channels` в `GET /subs` и `GET /admin/subs`: видимость — по НЫНЕШНЕМУ проекту беседы. Завершённые
    — за `ended_ms` (сутки; Ф92-1 B5 — `hub sub list --days N`)."""
    if live_only:
        recs = _select(conn, "ended_ts IS NULL", ())
    else:
        recs = _select(conn, "ended_ts IS NULL OR ended_ts >= ?", (now_ms - ended_ms,))
    recs.reverse()  # новые первыми, как `subs.list_subs`
    if project_ids is not None:
        kept = []
        for rec in recs:
            channel = _channel(conn, rec.channel_id)
            if visible(project_ids, channel.project_id if channel is not None else rec.project_id):
                kept.append(rec)
        recs = kept
    pending = pending_counts(conn, [r.id for r in recs])
    machines = machines_mod.Labels(conn, now_ms=now_ms)
    return [
        chan_json(
            rec,
            now_ms=now_ms,
            waiting=waiters.waiting(rec_key(rec), rec.session_id) if waiters is not None else False,
            pending=pending.get(rec.id, 0),
            admin=admin,
            me=me,
            role=_role(conn, rec),
            hub=held_mod.of_rec(conn, rec),
            machine=machines.of_rec(rec),
        )
        for rec in recs
    ]


# --- завершение -----------------------------------------------------------------------------------------


def end(
    conn,
    sub_id: int,
    reason: str,
    *,
    now_ms: int,
    actor: str,
    api_token_id: int | None = None,
    client_name: str | None = None,
    request_id: str | None = None,
) -> bool:
    """A2: `ended_ts`, `end_reason`, `pending` -> `ended`, аудит `chan_sub_end`; П19 — добор 13-1 по беседе
    (открытая карточка не ждёт). Уже завершённая — `False`, ничего не пишется."""
    if reason not in END_REASONS:
        raise ValueError(f"неизвестная причина завершения {reason!r}")
    cur = conn.execute(
        "UPDATE main.session_chan_sub SET ended_ts = ?, end_reason = ? WHERE id = ? AND ended_ts IS NULL",
        (now_ms, reason, sub_id),
    )
    if cur.rowcount == 0:
        return False
    conn.execute(
        "UPDATE main.session_chan_signal SET status = 'ended', updated_ts = ? "
        "WHERE chan_sub_id = ? AND status = 'pending'",
        (now_ms, sub_id),
    )
    channel_id = conn.execute(
        "SELECT channel_id FROM main.session_chan_sub WHERE id = ?", (sub_id,)
    ).fetchone()[0]
    write_audit(
        conn,
        now_ms=now_ms,
        actor=actor,
        api_token_id=api_token_id,
        client_name=client_name,
        request_id=request_id or uuid.uuid4().hex,
        action="chan_sub_end",
        result=reason,
        channel_id=channel_id,
        detail={"sub_id": sub_id, "channel_id": channel_id},
    )
    channel = _channel(conn, channel_id)
    rec = get(conn, sub_id)
    if channel is not None and channel.project_id is not None and rec is not None:
        # Ф26-1 A3: добор — подписке на проект ТОЙ ЖЕ сессии (подписки других сессий беседу и так слушали).
        row = conn.execute(
            "SELECT id FROM main.session_sub WHERE project_id = ? AND ended_ts IS NULL AND mode = 'cards' "
            "AND api_token_id = ? AND session_pid = ? AND session_proc_start = ? AND session_id = ?",
            (channel.project_id, *rec_key(rec)),
        ).fetchone()
        if row is not None:
            subs.backfill(conn, row[0], channel.project_id, now_ms=now_ms, channel_id=channel_id)
    return True


def cleanup(conn, *, now_ms: int, waiting=None) -> list[tuple[int, str]]:
    """Фоновая задача ядра (вместе с `subs.cleanup`): токен отозван или истёк — `token_revoked`; процесс умер
    или подменён — `session_gone`. Возвращает `(sub_id, причина)` завершённых. Ф16-4 (П1): брошенная подписка
    OpenCode (`subs.oc_stale`; `waiting(rec)` — идёт ли ожидание этой сессии) — тоже `session_gone`."""
    ended: list[tuple[int, str]] = []
    for rec in _select(conn, "ended_ts IS NULL", ()):
        sub_id = rec.id
        token = conn.execute(
            "SELECT id, revoked, expires_ts FROM main.api_token WHERE id = ?", (rec.api_token_id,)
        ).fetchone()
        if token is None or token[1] or (token[2] is not None and token[2] <= now_ms):
            reason = "token_revoked"
        elif not subs.rec_session_alive(conn, rec):
            reason = "session_gone"  # Ф41-2а П2: сессия хаба — по базе (пустой реестр ничего не завершает)
        elif subs.oc_stale(rec, now_ms, waiting=bool(waiting and waiting(rec))):
            reason = "session_gone"
        else:
            subs.refresh_local_seen(conn, "session_chan_sub", rec, now_ms=now_ms)  # Ф33, правка 17.10
            continue
        if end(conn, sub_id, reason, now_ms=now_ms, actor="core"):
            ended.append((sub_id, reason))
    return ended


# --- подписка -------------------------------------------------------------------------------------------


def subscribe(
    conn,
    *,
    principal,
    session: dict,
    channel_id: int,
    now_ms: int,
    client_name: str | None,
    request_id: str,
    keywords: list | None = None,
    exclude: list | None = None,
    batch_min: int | None = None,
) -> tuple[ChanSubRec, bool]:
    """`POST /chan-subs` одной транзакцией (A2 + П1, П5, П14). Права токена (скоуп, вид, имя) проверяет
    маршрут. Возвращает `(подписка, создана ли)`. Отказ — `subs.SubError`. Добора нет.

    Ф31-1 решение 4 + П3: `keywords`/`exclude` — ключи подписки (нормализация и пределы — как у подписки на
    проект). Своя живая подписка беседы: `keywords` не `None` — ключи меняются на месте (аудит `chan_sub_keys`
    числами), `[]` — сняты; `None` — как есть. Наследования ключей у подписки на беседу нет.

    Ф36-1 A3 + П4: `batch_min` — интервал пачки сигналов «по ключу», внутри `keywords_json`. С `keywords` —
    пишется (не задан — остаётся прежний интервал своей живой подписки); без `keywords` у своей живой с
    ключами — меняется только интервал; ключей в итоге нет — 422.

    Ф39 A0/A2 п.2: живая подписка — одна на пару (беседа, сессия); сессий у беседы сколько угодно, отказа
    «беседу уже слушает сессия» нет. Та же сессия под другим токеном — прежняя `replaced`."""
    kw_json = subs.keywords_spec(keywords, exclude)
    subs.check_batch_min(batch_min)
    if batch_min is not None:
        kw_json = keywords_mod.with_batch(kw_json, batch_min)  # Ф67-1: и без ключей
    pid, proc_start, session_id = session["pid"], session["proc_start"], session["session_id"]
    kind = session.get("kind")
    if kind == held_mod.KIND or held_mod.is_held(pid):
        # Ф41-2а П4: тройка сессии хаба согласована и сессия не закрыта (по базе) — до любых записей.
        subs.hub_session_of(conn, session)
    elif not subs.is_alive(pid, proc_start, session.get("host")):
        raise subs.SubError(409, "сессия не найдена")
    if kind not in subs.SESSION_KINDS:
        raise subs.SubError(403, "подписываться может только интерактивная сессия")
    channel = _channel(conn, channel_id)
    if channel is None or not visible(principal.project_ids, channel.project_id):
        raise subs.SubError(404, "not found")
    if channel.archived:
        # 24.10: беседа-заготовка (первое сообщение новому человеку) — подписка разрешена: быстрый ответ
        # заказчика роль узнаёт сразу. Заготовка станет настоящей беседой — тот же `channel_id` (`_rename`)
        # или подписка переедет вместе с лентой (`draft_channels._merge` -> `on_channel_migrate`).
        raise subs.SubError(422, "беседа в архиве — подписка невозможна")

    audit = {"actor": principal.kind, "api_token_id": principal.token_id, "client_name": client_name}
    # Ф99-0: разговор Codex/Cursor с новым процессом — его живые подписки сначала переезжают на этот процесс.
    rebind_audit = {**audit, "request_id": request_id}
    subs.ext_rebind(conn, token_id=principal.token_id, session=session, now_ms=now_ms, audit=rebind_audit)
    # П5: новый разговор того же процесса снимает ВСЕ подписки прежнего разговора. Ф16-1 (П1): правило —
    # только для сессии Claude Code; у OpenCode на одном pid живут разные живые сессии, их подписки не наши.
    if not subs.many_convs(session_id, kind):
        for rec in live_of_process(conn, pid, proc_start):
            if rec.session_id != session_id:
                end(conn, rec.id, "conversation_changed", now_ms=now_ms, request_id=request_id, **audit)

    # Ф39 A2 п.2: подписка на беседу своя у каждой сессии — чужие живые подписки беседы не мешают и не
    # трогаются (отказа «беседу уже слушает сессия» нет; мёртвого держателя снимет `cleanup`).
    key = session_key(principal.token_id, pid, proc_start, session_id)
    prev = live_of_session(conn, channel_id, key)
    if prev is not None:
        if keywords is not None:
            if batch_min is None:
                # П4(а): прежний интервал подмешивается ДО сравнения — повтор с теми же ключами ничего
                # не меняет и срок ждущих сигналов не сбрасывает.
                kw_json = keywords_mod.with_batch(kw_json, keywords_mod.stored_batch(prev.keywords_json))
        elif batch_min is not None:
            # П4(б): только интервал — ключи своей живой подписки остаются (Ф67-1: и у подписки без ключей).
            kw_json = keywords_mod.with_batch(prev.keywords_json, batch_min)
        else:
            kw_json = prev.keywords_json
        if kw_json != prev.keywords_json:
            _set_keywords(conn, prev, kw_json, now_ms=now_ms, request_id=request_id, **audit)
            prev = get(conn, prev.id)
            assert prev is not None  # noqa: S101 — живая строка этой транзакции
        return prev, False
    # Та же сессия (pid, `proc_start`, `session_id`) под ДРУГИМ токеном держит живую подписку беседы — она
    # снимается `replaced` (после проверки предела: отказ ничего не пишет).
    replaced = [
        rec
        for rec in live_subs_of_channel(conn, channel_id)
        if subs.same_session(rec, pid, proc_start, session_id)
    ]
    gone = {rec.id for rec in replaced}
    count = sum(
        1
        for rec in live_of_process(conn, pid, proc_start)
        if rec.session_id == session_id and rec.id not in gone
    )
    if count >= MAX_CHAN_SUBS:
        raise subs.SubError(
            409, f"сессия уже слушает {MAX_CHAN_SUBS} бесед — сначала hub_unsubscribe(channel_id=…)"
        )
    for rec in replaced:
        end(conn, rec.id, "replaced", now_ms=now_ms, request_id=request_id, **audit)

    rec = _insert(
        conn,
        channel_id=channel_id,
        project_id=channel.project_id,
        token_id=principal.token_id,
        session=session,
        keywords_json=kw_json,
        now_ms=now_ms,
        request_id=request_id,
        action="chan_sub_create",
        audit=audit,
    )
    return rec, True


class ForSub(NamedTuple):
    """Итог `subscribe_for_sub`: `rec` — подписка на беседу (новая или уже бывшая), `outcome` — `created`,
    `exists` или причина отказа (`not_visible`, `archived`, `limit`, `session_gone`)."""

    rec: ChanSubRec | None
    outcome: str


def subscribe_for_sub(
    conn, sub, channel_id: int, *, now_ms: int, request_id: str, action: str, detail: dict | None = None
) -> ForSub:
    """24.10 (просьба менеджера «Hooli»): ядро само подписывает сессию живой подписки на проект `sub`
    (`subs.SubRec`) на беседу `channel_id` — как `POST /chan-subs` без ключей, от её же токена. Своя живая
    подписка беседы — она и остаётся (`exists`). Отказы — причиной, без записи: беседа не видна токену
    подписки или в архиве, у сессии уже `MAX_CHAN_SUBS` бесед, процесс сессии не жив. Аудит — `action` с
    `detail`."""
    if sub.ended_ts is not None or not subs.rec_session_alive(conn, sub):
        return ForSub(None, "session_gone")
    for rec in live_subs_of_channel(conn, channel_id):
        if subs.same_session(rec, sub.session_pid, sub.session_proc_start, sub.session_id):
            return ForSub(rec, "exists")
    token = conn.execute(
        "SELECT project_scope_json FROM main.api_token WHERE id = ? AND revoked = 0", (sub.api_token_id,)
    ).fetchone()
    channel = _channel(conn, channel_id)
    scope = frozenset(json.loads(token[0])) if token is not None and token[0] is not None else None
    if token is None or channel is None or not visible(scope, channel.project_id):
        return ForSub(None, "not_visible")
    if channel.archived:
        return ForSub(None, "archived")
    count = sum(
        1
        for rec in live_of_process(conn, sub.session_pid, sub.session_proc_start)
        if rec.session_id == sub.session_id
    )
    if count >= MAX_CHAN_SUBS:
        return ForSub(None, "limit")
    boot = conn.execute("SELECT session_boot FROM main.session_sub WHERE id = ?", (sub.id,)).fetchone()
    session = {
        "pid": sub.session_pid,
        "proc_start": sub.session_proc_start,
        "session_id": sub.session_id,
        "cwd": sub.session_cwd,
        "host": sub.session_host,
        "boot": boot[0] if boot is not None else None,
    }
    if sub.session_name:
        session.update(name=sub.session_name, name_source="user")
    rec = _insert(
        conn,
        channel_id=channel_id,
        project_id=channel.project_id,
        token_id=sub.api_token_id,
        session=session,
        keywords_json=None,
        now_ms=now_ms,
        request_id=request_id,
        action=action,
        audit={"actor": "core", "api_token_id": sub.api_token_id, "client_name": None},
        detail={"project_sub_id": sub.id, **(detail or {})},
        engine=sub.session_engine,
    )
    return ForSub(rec, "created")


def _insert(
    conn,
    *,
    channel_id: int,
    project_id: int | None,
    token_id: int,
    session: dict,
    keywords_json: str | None,
    now_ms: int,
    request_id: str,
    action: str,
    audit: dict,
    detail: dict | None = None,
    engine: str | None = None,
) -> ChanSubRec:
    """Вставка строки подписки на беседу и её аудит — общая для `subscribe` (`chan_sub_create`) и
    `restore_for_role` (Ф32-1, `chan_sub_restore`). Проверки — у вызывающего."""
    pid, proc_start, session_id = session["pid"], session["proc_start"], session["session_id"]
    name = session.get("name")
    session_name = name[: subs.SESSION_NAME_MAX] if name and session.get("name_source") == "user" else None
    cur = conn.execute(
        "INSERT INTO main.session_chan_sub (channel_id, project_id, api_token_id, session_pid, "
        "session_proc_start, session_id, session_cwd, session_name, created_ts, keywords_json, session_host, "
        "session_boot, session_seen_ts, session_engine) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            channel_id,
            project_id,
            token_id,
            pid,
            proc_start,
            session_id,
            session["cwd"],
            session_name,
            now_ms,
            keywords_json,
            session.get("host"),  # Ф33-1: машина сессии (NULL — своя)
            session.get("boot"),  # Ф33-1: её загрузка (NULL — клиент не прислал)
            session_host_mod.seen_at_insert(session, now_ms),  # Ф33-1: контакт
            engine or subs.engine_for_insert(conn, session),  # Ф74-2: движок сессии
        ),
    )
    sub_id = cur.lastrowid
    write_audit(
        conn,
        now_ms=now_ms,
        request_id=request_id,
        action=action,
        result="ok",
        channel_id=channel_id,
        detail={
            "sub_id": sub_id,
            "channel_id": channel_id,
            "project_id": project_id,
            "pid": pid,
            "session_id8": session_id[: subs.SESSION_ID8],
            **_keys_counts(keywords_json),
            **(detail or {}),
        },
        **audit,
    )
    rec = get(conn, sub_id)
    assert rec is not None  # noqa: S101 — только что вставлена в этой транзакции
    return rec


# --- возврат подписок на беседы по роли (Ф32-1) ---------------------------------------------------------


class Restored(NamedTuple):
    """Итог `restore_for_role`: `restored` — `{chan_sub_id, channel_id, keys, exclude}` (числа ключей),
    `skipped` — `{channel_id, reason}` (`busy:#<подписка>`, `limit`, `not_visible`, `archived`), `blocked` —
    почему не возвращалось ничего: причина конца источника роли (П7) или `old` (П6); `None` — возвращалось."""

    restored: list[dict]
    skipped: list[dict]
    blocked: str | None = None


def same_owner_reasons(rec, session: dict) -> tuple[str, ...]:
    """Ф92-1 (B3): строка `rec` (подписка на проект или на беседу) — той же сессии, что запрос (условная
    тройка совпадает целиком, токен мог смениться), — причины конца, с которыми её беседы переходят ей:
    у сессии хаба `SAME_HELD_REASONS` (Ф41-2а П7), у внешней `SAME_EXT_REASONS`; чужая сессия — `()`."""
    if not subs.same_session(rec, session["pid"], session["proc_start"], session["session_id"]):
        return ()
    return SAME_HELD_REASONS if held_mod.is_held(rec.session_pid) else SAME_EXT_REASONS


def _role_holder(conn, project_id: int, role_key: str, channel_id: int, new_key: tuple) -> int | None:
    """Ф92-1 (B2): живая подписка на беседу `channel_id`, чья сессия держит живую подписку роли `role_key` в
    проекте `project_id` (любая линия, кроме сессии-получателя `new_key`), — её id; нет — `None`."""
    row = conn.execute(
        "SELECT c.id FROM main.session_chan_sub c JOIN main.session_sub s "
        "ON s.api_token_id = c.api_token_id AND s.session_pid = c.session_pid "
        "AND s.session_proc_start = c.session_proc_start AND s.session_id = c.session_id "
        "WHERE c.channel_id = ? AND c.ended_ts IS NULL AND s.ended_ts IS NULL AND s.project_id = ? "
        "AND s.role_key = ? AND NOT (c.api_token_id = ? AND c.session_pid = ? AND c.session_proc_start = ? "
        "AND c.session_id = ?) ORDER BY c.id LIMIT 1",
        (channel_id, project_id, role_key, *new_key),
    ).fetchone()
    return row[0] if row is not None else None


def audit_restore_blocked(
    conn,
    *,
    principal,
    sub_id: int | None,
    source_id: int | None,
    blocked: str,
    now_ms: int,
    client_name: str | None,
    request_id: str,
) -> None:
    """Ф92-1 (B4): возврат бесед не состоялся целиком — строка аудита `chan_sub_restore` с `result: blocked`
    (`no_source` — у подписки с ролью нет прежней подписки роли; иначе причина конца источника или `old`)."""
    write_audit(
        conn,
        now_ms=now_ms,
        actor=principal.kind,
        api_token_id=principal.token_id,
        client_name=client_name,
        request_id=request_id,
        action="chan_sub_restore",
        result="blocked",
        detail={
            **({"sub_id": sub_id} if sub_id is not None else {}),
            **({"source": source_id} if source_id is not None else {}),
            "blocked": blocked,
        },
    )


def _source_closable(
    conn, rec: ChanSubRec, session: dict, source_reason: str | None, *, now_ms: int, waiting
) -> str | None:
    """П1 + П7: можно ли закрыть ЖИВУЮ подписку на беседу идентичности источника — причина закрытия или
    `None` (источник жив: не трогать). Claude Code — процесс мёртв (`session_gone`) или тот разговор ушёл
    (источник на проект кончился `conversation_changed`, либо тот же процесс уже с другим `session_id`).
    OpenCode — только «мёртв» как в `cleanup` (на одном процессе много живых сессий). Ф41-2а П7: строка той
    же сессии хаба под прежним токеном — `replaced` (её сменяет подписка новым токеном)."""
    if same_owner_reasons(rec, session):
        return "replaced"  # Ф92-1 (B3): и внешняя сессия под новым токеном
    alive = subs.rec_session_alive(conn, rec)
    if subs.rec_many_convs(rec):
        stale = subs.oc_stale(rec, now_ms, waiting=bool(waiting and waiting(rec)))
        return "session_gone" if not alive or stale else None
    if not alive:
        return "session_gone"
    same_proc = rec.session_pid == session["pid"] and rec.session_proc_start == session["proc_start"]
    changed = (
        same_proc
        and rec.session_id != session["session_id"]
        and not subs.many_convs(session["session_id"], session.get("kind"))
    )
    if source_reason == "conversation_changed" or changed:
        return "conversation_changed"
    return None


def restore_for_role(
    conn,
    *,
    principal,
    session: dict,
    source_sub_id: int,
    now_ms: int,
    source_reason: str | None = None,
    client_name: str | None = None,
    request_id: str | None = None,
    waiting=None,
    role_key: str | None = None,
    sub_id: int | None = None,
) -> Restored:
    """Ф32-1: новая подписка на проект с ролью возвращает подписки на беседы прежней сессии этой роли — в той
    же транзакции `POST /subs`. `source_sub_id` — источник роли (`subs.Subscribed.role_source_id`),
    `source_reason` — почему он кончился (нет — из строки), `waiting(rec)` — идёт ли ожидание бесед сессии
    (брошенная подписка OpenCode, как в `cleanup`).

    Кандидат — последняя строка беседы СРЕДИ строк идентичности источника (Ф39 A2 п.4: чужие подписки беседы
    возврату не мешают): живая или снятая с причиной из `RESTORE_REASONS`. Ф39 П1 + П2: кандидат молча
    отпадает (ни в `restored`, ни в `skipped`, без записи), если у сессии-получателя есть своя живая подписка
    беседы или ЛЮБАЯ её строка беседы новее последней строки источника (беседу уже возвращали — дальше решает
    сама сессия; иначе явная отписка возвращалась бы каждым `hub_subscribe()`). Порядок записи на кандидата
    (П9; один `pending` на подписку): `end` живой строки источника -> предпроверки (П6:
    пропуск без побочных эффектов; П10: сигнал невосстановленного остаётся закрытым) -> вставка новой ->
    сигнал источника, закрытый вместе с ним, возвращается в `pending` новой (П2а) -> пометки «ключ сработал»
    (П8) -> добор окна через `_put` (П2б). Отказов не бросает: ошибка одной беседы не роняет подписку на
    проект.

    Ф92-1: `role_key` задан — источник взят с ДРУГОЙ линии роли (B2): беседу, которую держит живая сессия этой
    роли, не трогаем (`busy:#<её подписка>`). Источник — та же сессия (B3: тот же токен — её подписки снимало
    ядро, а она жива; другой токен — `same_owner_reasons`) — её снятые «сами» беседы возвращаются. `sub_id` —
    новая подписка на проект: задан — отказ целиком пишется в аудит (у повторного вызова не задают)."""
    source = subs.get_sub(conn, source_sub_id)
    if source is None or source.ended_ts is None:
        return Restored([], [])
    request_id = request_id or uuid.uuid4().hex
    audit = {"actor": principal.kind, "api_token_id": principal.token_id, "client_name": client_name}
    pid, proc_start, session_id = session["pid"], session["proc_start"], session["session_id"]
    key = rec_key(source)
    # Ф92-1 (B3): раньше та же сессия (тот же ключ) сразу получала пустой ответ — «подписки и так при ней»; но
    # их могло снять ядро (`session_gone` по окну «не в сети»), а сессия жива. Её живые строки отсеет проверка
    # `own` ниже, снятые «сами» — вернутся.
    mine = _select(
        conn,
        "api_token_id = ? AND session_pid = ? AND session_proc_start = ? AND session_id = ?",
        key,
    )
    new_key = session_key(principal.token_id, pid, proc_start, session_id)
    # Ф41-2а П7 + Ф92-1 (B3): та же сессия (хаба или внешняя) под другим токеном — свои причины возврата.
    same = same_owner_reasons(source, session)
    # `mine` — по `id`: в словаре остаётся последняя строка беседы у источника.
    last_of: dict[int, ChanSubRec] = {rec.channel_id: rec for rec in mine}
    candidates: list[ChanSubRec] = []
    for channel_id in sorted(last_of):
        last = last_of[channel_id]
        if last.ended_ts is not None and not (last.end_reason in RESTORE_REASONS or last.end_reason in same):
            continue
        own = _select(
            conn,
            "channel_id = ? AND api_token_id = ? AND session_pid = ? AND session_proc_start = ? "
            "AND session_id = ? AND (ended_ts IS NULL OR id > ?)",
            (channel_id, *new_key, last.id),
        )
        if own:
            continue  # Ф39 П1/П2: своя живая или беседу этой сессии уже возвращали — молча
        candidates.append(last)
    if not candidates:
        return Restored([], [])
    reason = source_reason or source.end_reason
    blocked = None
    if reason not in RESTORE_REASONS and reason not in same:
        blocked = reason  # П7
    elif now_ms - source.ended_ts > RESTORE_MAX_AGE_MS:
        blocked = "old"  # П6
    if blocked is not None:
        if sub_id is not None:
            audit_restore_blocked(
                conn,
                principal=principal,
                sub_id=sub_id,
                source_id=source.id,
                blocked=blocked,
                now_ms=now_ms,
                client_name=client_name,
                request_id=request_id,
            )
        return Restored([], [], blocked)
    candidates.sort(key=lambda rec: rec.id, reverse=True)  # свежие первыми
    restored: list[dict] = []
    skipped: list[dict] = []
    for old in candidates:
        channel_id = old.channel_id
        if role_key is not None:
            holder = _role_holder(conn, source.project_id, role_key, channel_id, new_key)
            if holder is not None:
                skipped.append({"channel_id": channel_id, "reason": f"busy:#{holder}"})
                continue
        if old.ended_ts is None:
            close = _source_closable(conn, old, session, reason, now_ms=now_ms, waiting=waiting)
            if close is None:
                skipped.append({"channel_id": channel_id, "reason": f"busy:#{old.id}"})
                continue
            end(conn, old.id, close, now_ms=now_ms, request_id=request_id, **audit)
            old = get(conn, old.id)  # noqa: PLW2901 — та же строка, уже с `ended_ts`
            assert old is not None  # noqa: S101 — строка этой транзакции
        channel = _channel(conn, channel_id)
        count = sum(1 for rec in live_of_process(conn, pid, proc_start) if rec.session_id == session_id)
        if channel is None or not visible(principal.project_ids, channel.project_id):
            skip = "not_visible"
        elif channel.archived:  # 24.10: заготовку не выбрасываем — подписка на неё разрешена
            skip = "archived"
        elif count >= MAX_CHAN_SUBS:
            skip = "limit"
        else:
            skip = None
        if skip is not None:
            skipped.append({"channel_id": channel_id, "reason": skip})
            continue
        new = _insert(
            conn,
            channel_id=channel_id,
            project_id=channel.project_id,
            token_id=principal.token_id,
            session=session,
            keywords_json=old.keywords_json,
            now_ms=now_ms,
            request_id=request_id,
            action="chan_sub_restore",
            audit=audit,
            detail={"source": old.id},
        )
        # П2а + П9: сигнал, закрытый вместе с подпиской источника (`updated_ts` = её `ended_ts`), — снова
        # `pending`, уже новой подписки; у новой других `pending` ещё нет. Ф36-1 B7: срок пачки (`ready_ts`)
        # при переносе сохраняется.
        waited = conn.execute(
            "SELECT id FROM main.session_chan_signal WHERE chan_sub_id = ? AND status = 'ended' "
            "AND updated_ts = ? ORDER BY id DESC LIMIT 1",
            (old.id, old.ended_ts),
        ).fetchone()
        if waited is not None:
            conn.execute(
                "UPDATE main.session_chan_signal SET chan_sub_id = ?, status = 'pending', updated_ts = ? "
                "WHERE id = ?",
                (new.id, now_ms, waited[0]),
            )
        # П8: пометки «ключ сработал» переезжают — правка сообщения, о котором источник уже получил сигнал,
        # новую подписку не будит.
        conn.execute(
            "INSERT OR IGNORE INTO main.sub_keyword_hit (sub_kind, sub_id, message_id, ts) "
            "SELECT 'channel', ?, message_id, ts FROM main.sub_keyword_hit "
            "WHERE sub_kind = 'channel' AND sub_id = ?",
            (new.id, old.id),
        )
        _restore_window(conn, old, new, source_sub_id=source.id, now_ms=now_ms)
        keys, minus = keywords_mod.spec_counts(new.keywords_json)
        restored.append({"chan_sub_id": new.id, "channel_id": channel_id, "keys": keys, "exclude": minus})
    return Restored(restored, skipped)


def _restore_window(conn, old: ChanSubRec, new: ChanSubRec, *, source_sub_id: int, now_ms: int) -> int:
    """П2б: чужие входящие беседы, пришедшие в окне «источник снят -> новая подписка» (`ts` не раньше
    `ended_ts` источника с допуском, не старше суток, не больше 200 самых свежих) и не дававшие сигнала этой
    цепочке (вставлены позже всех сообщений сигналов источника — его подписки на беседу и на проект
    `source_sub_id`), — по правилам подписки на беседу в `pending` новой, только через `_put` (одна строка).
    Число сообщений, давших сигнал."""
    schema = db_of(new.channel_id)
    seen = conn.execute(
        "SELECT MAX(MAX(first_message_id, last_message_id)) FROM main.session_chan_signal "
        "WHERE (chan_sub_id IN (?, ?) OR project_sub_id = ?) AND channel_id = ?",
        (old.id, new.id, source_sub_id, new.channel_id),
    ).fetchone()[0]
    # Ревью: `ts` — время источника, а в хаб сообщение приходит позже: допуск назад как у обычного сигнала
    # (`SIGNAL_TS_SLACK_MS`), но не раньше самой подписки источника; от дублей — условие `id > seen`.
    since = max(old.ended_ts - SIGNAL_TS_SLACK_MS, old.created_ts)
    rows = conn.execute(
        f"SELECT id, ts FROM {schema}.message WHERE channel_id = ? AND is_me = 0 AND hidden = 0 "  # noqa: S608
        "AND deleted = 0 AND kind != 'event' AND ts > ? AND ts >= ? AND id > ? "
        "ORDER BY ts DESC, id DESC LIMIT ?",
        (new.channel_id, since, now_ms - RESTORE_WINDOW_MS, seen or 0, RESTORE_WINDOW_MAX),
    ).fetchall()
    count = 0
    for message_id, ts in reversed(rows):
        info = message_cat(conn, message_id)
        text = _message_text(conn, message_id) if keywords_mod.has_keys(new.keywords_json) else None
        if _signal_chan(
            conn, new, new.channel_id, message_id, ts=ts, now_ms=now_ms, recheck=False, info=info, text=text
        ):
            count += 1
    return count


def _keys_counts(keywords_json: str | None) -> dict:
    """Аудит — только числа ключей (тексты ключей в аудит не пишутся)."""
    if keywords_json is None:
        return {}
    keys, minus = keywords_mod.spec_counts(keywords_json)
    batch_min = keywords_mod.stored_batch(keywords_json)
    return {"keys": keys, "exclude": minus, **({"batch_min": batch_min} if batch_min is not None else {})}


def _set_keywords(
    conn,
    rec: ChanSubRec,
    keywords_json: str | None,
    *,
    now_ms: int,
    actor: str,
    api_token_id: int | None,
    client_name: str | None,
    request_id: str,
) -> None:
    """Ф31-1 П3: смена ключей своей живой подписки на беседу на месте; `pending` выдаётся как есть.
    Ф36-1 B4: любое изменение `keywords_json` (ключи, минус-слова, интервал, снятие) обнуляет срок пачки
    ждущих строк — сигнал уходит ближайшей выдачей."""
    conn.execute("UPDATE main.session_chan_sub SET keywords_json = ? WHERE id = ?", (keywords_json, rec.id))
    conn.execute(
        "UPDATE main.session_chan_signal SET ready_ts = NULL "
        "WHERE chan_sub_id = ? AND status = 'pending' AND ready_ts IS NOT NULL",
        (rec.id,),
    )
    keys, minus = keywords_mod.spec_counts(keywords_json)
    batch_min = keywords_mod.stored_batch(keywords_json)
    write_audit(
        conn,
        now_ms=now_ms,
        actor=actor,
        api_token_id=api_token_id,
        client_name=client_name,
        request_id=request_id,
        action="chan_sub_keys",
        result="ok",
        channel_id=rec.channel_id,
        detail={
            "sub_id": rec.id,
            "channel_id": rec.channel_id,
            "keys": keys,
            "exclude": minus,
            **({"batch_min": batch_min} if batch_min is not None else {}),
        },
    )


# --- сигналы (A3, П20) ----------------------------------------------------------------------------------


def _message_ts(conn, message_id: int) -> int | None:
    schema = db_of(message_id)
    row = conn.execute(f"SELECT ts FROM {schema}.message WHERE id = ?", (message_id,)).fetchone()  # noqa: S608
    return row[0] if row is not None else None


def _put(
    conn,
    *,
    chan_sub_id: int | None,
    project_sub_id: int | None,
    channel_id: int,
    message_id: int,
    ts: int,
    now_ms: int,
    cat: str = "other",
    thread_ext: str | None = None,
    keys: list[str] | None = None,
    recheck: bool = False,
    batch_ms: int = 0,
    rules: list[int] | None = None,
) -> None:
    """Один `pending` на подписку (на проект — на подписку и беседу): вставить или дописать число и последнее
    сообщение (последнее — самое позднее по времени источника: сверка может вставить старое после нового).
    Ф20-1 П10: `cat` — сильнейшая из накопленных, `thread_ext` — у сообщения, давшего её (при равенстве — у
    последнего пришедшего). Ф25-1 A4: `keys` — совпавшие ключи; при дописывании — объединение со старыми
    (порядок первого появления, не больше `SIGNAL_KEYS_MAX`), независимо от категории.

    Ф32-2 п.7 + П7: `recheck` (перепроверка ключей) — сообщение, уже входящее в ждущую строку этой подписки
    (`first_message_id ≤ id ≤ last_message_id` той же беседы), второй раз не считается: строке только
    категория и ключи. Диапазон по id неточен (`last_message_id` — последнее по времени источника): сигнал не
    теряется, число может быть занижено на одно сообщение.

    Ф36-1 B2: `batch_ms` — интервал пачки подписки (0 — пачки нет). Новая строка категории `keyword` при
    `batch_ms > 0` получает срок `ready_ts` (`batch_ready_ts`), иначе `NULL` — готова сразу. Дописывание:
    итоговая категория `keyword` — срок не меняется (был `NULL` — остаётся: строка уже готова), итоговая
    личная — `ready_ts = NULL` (личное уходит мгновенно и забирает накопленное). То же при `recheck`.
    Ф67-1: то же для категории `other` (подписка «все сообщения» или на беседу без ключей) — `BATCH_CATS`.

    Ф63-1 П4: `rules` — номера правил входящих, разбудивших подписку; при дописывании — объединение (не
    больше 5). Сигнал с правилом идёт с `batch_ms = 0`: строка готова сразу и забирает накопленное."""
    if chan_sub_id is not None:
        row = conn.execute(
            "SELECT id, last_message_id, cat, keys_json, first_message_id, channel_id, rules_json "
            "FROM main.session_chan_signal WHERE chan_sub_id = ? AND status = 'pending'",
            (chan_sub_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id, last_message_id, cat, keys_json, first_message_id, channel_id, rules_json "
            "FROM main.session_chan_signal "
            "WHERE project_sub_id = ? AND channel_id = ? AND status = 'pending'",
            (project_sub_id, channel_id),
        ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO main.session_chan_signal (chan_sub_id, project_sub_id, channel_id, "
            "first_message_id, last_message_id, msg_count, status, created_ts, updated_ts, cat, thread_ext, "
            "keys_json, ready_ts, rules_json) VALUES (?, ?, ?, ?, ?, 1, 'pending', ?, ?, ?, ?, ?, ?, ?)",
            (
                chan_sub_id,
                project_sub_id,
                channel_id,
                message_id,
                message_id,
                now_ms,
                now_ms,
                cat,
                thread_ext,
                _keys_json(None, keys),
                batch_ready_ts(now_ms, batch_ms) if cat in BATCH_CATS and batch_ms > 0 else None,
                _rules_json(None, rules),
            ),
        )
        return
    signal_id, last_id, old_cat, old_keys, first_id, row_channel, old_rules = row
    keys_json = _keys_json(old_keys, keys)
    if rules:
        # Ф63-1 П4: номера правил — объединением, отдельной записью (остальные ветки их не трогают).
        conn.execute(
            "UPDATE main.session_chan_signal SET rules_json = ? WHERE id = ?",
            (_rules_json(old_rules, rules), signal_id),
        )
    # Ф36-1 B2 + Ф67-1: срок пачки остаётся как был, только пока строку дописывает сообщение категории пачки
    # (`other`/`keyword`) подписки с пачкой. Личное и подписка без пачки срок снимают (ревью Ф36-1:
    # перенесённая строка не должна держать срок прежней подписки).
    ready = "" if cat in BATCH_CATS and batch_ms > 0 else ", ready_ts = NULL"
    if recheck and row_channel == channel_id and first_id <= message_id <= last_id:
        if CAT_RANK.get(cat, 0) >= CAT_RANK.get(old_cat or "other", 0):
            conn.execute(
                "UPDATE main.session_chan_signal SET updated_ts = ?, cat = ?, thread_ext = ?, "  # noqa: S608
                f"keys_json = ?{ready} WHERE id = ?",
                (now_ms, cat, thread_ext, keys_json, signal_id),
            )
        else:
            conn.execute(
                f"UPDATE main.session_chan_signal SET updated_ts = ?, keys_json = ?{ready} WHERE id = ?",  # noqa: S608
                (now_ms, keys_json, signal_id),
            )
        return
    last_ts = _message_ts(conn, last_id)
    newest = message_id if last_ts is None or ts >= last_ts else last_id
    if CAT_RANK.get(cat, 0) >= CAT_RANK.get(old_cat or "other", 0):
        conn.execute(
            "UPDATE main.session_chan_signal SET last_message_id = ?, msg_count = msg_count + 1, "  # noqa: S608
            f"updated_ts = ?, cat = ?, thread_ext = ?, keys_json = ?{ready} WHERE id = ?",
            (newest, now_ms, cat, thread_ext, keys_json, signal_id),
        )
        return
    conn.execute(
        "UPDATE main.session_chan_signal SET last_message_id = ?, msg_count = msg_count + 1, "  # noqa: S608
        f"updated_ts = ?, keys_json = ?{ready} WHERE id = ?",
        (newest, now_ms, keys_json, signal_id),
    )


def _load_keys(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError, TypeError:
        return []
    return [k for k in value if isinstance(k, str)] if isinstance(value, list) else []


def _keys_json(old: str | None, new: list[str] | None) -> str | None:
    """Ф25-1 A4: объединение ключей сигнала — порядок первого появления, не больше `SIGNAL_KEYS_MAX`."""
    merged = _load_keys(old)
    for key in new or []:
        if key not in merged:
            merged.append(key)
    merged = merged[: keywords_mod.SIGNAL_KEYS_MAX]
    return json.dumps(merged, ensure_ascii=False) if merged else None


def _rules_json(old: str | None, new: list[int] | None) -> str | None:
    """Ф63-1 П4: объединение номеров правил сигнала — порядок первого появления, не больше 5."""
    merged = load_rules(old)
    for rule_id in new or []:
        if rule_id not in merged:
            merged.append(rule_id)
    merged = merged[:RULES_MAX_IN_SIGNAL]
    return json.dumps(merged) if merged else None


def load_rules(raw: str | None) -> list[int]:
    """Номера правил входящих сигнала (`rules_json`) — строгая форма, не больше 5."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError, TypeError:
        return []
    if not isinstance(value, list):
        return []
    rules = [v for v in value if isinstance(v, int) and not isinstance(v, bool) and v >= 1]
    return rules[:RULES_MAX_IN_SIGNAL]


def _put_filtered(
    conn,
    *,
    project_sub_id: int | None = None,
    chan_sub_id: int | None = None,
    channel_id: int,
    message_id: int,
    ts: int,
    reason: str,
    now_ms: int,
) -> None:
    """Ф20-1 П6: отфильтрованное подпиской на проект — строка `skipped` с причиной, одна на (подписка, беседа,
    причина): новая отфильтрованная дописывает в неё последнее сообщение и число (таблица не растёт). Ф31-1
    решение 4: так же у подписки на беседу с ключами (`chan_sub_id`, причина `no_keyword`)."""
    if chan_sub_id is not None:
        column, sub_id = "chan_sub_id", chan_sub_id
    else:
        column, sub_id = "project_sub_id", project_sub_id
    row = conn.execute(
        f"SELECT id, last_message_id FROM main.session_chan_signal "  # noqa: S608 — имя колонки константа
        f"WHERE {column} = ? AND channel_id = ? AND status = 'skipped' AND reason = ? "
        "ORDER BY id DESC LIMIT 1",
        (sub_id, channel_id, reason),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO main.session_chan_signal (chan_sub_id, project_sub_id, channel_id, "
            "first_message_id, last_message_id, msg_count, status, reason, created_ts, updated_ts) "
            "VALUES (?, ?, ?, ?, ?, 1, 'skipped', ?, ?, ?)",
            (
                chan_sub_id,
                project_sub_id if chan_sub_id is None else None,
                channel_id,
                message_id,
                message_id,
                reason,
                now_ms,
                now_ms,
            ),
        )
        return
    signal_id, last_id = row
    last_ts = _message_ts(conn, last_id)
    newest = message_id if last_ts is None or ts >= last_ts else last_id
    conn.execute(
        "UPDATE main.session_chan_signal SET last_message_id = ?, msg_count = msg_count + 1, updated_ts = ? "
        "WHERE id = ?",
        (newest, now_ms, signal_id),
    )


def _hit_seen(conn, sub_kind: str, sub_id: int, message_id: int) -> bool:
    """Ф31-1 решение 2: подписка уже получила сигнал «по ключу» об этом сообщении."""
    row = conn.execute(
        "SELECT 1 FROM main.sub_keyword_hit WHERE sub_kind = ? AND sub_id = ? AND message_id = ?",
        (sub_kind, sub_id, message_id),
    ).fetchone()
    return row is not None


def _hit_add(conn, sub_kind: str, sub_id: int, message_id: int, *, now_ms: int) -> None:
    """Ф31-1 решение 2 + П8: пометка «ключ сработал» — при каждом сигнале `keyword`; строки старше
    `KEYWORD_HIT_KEEP_MS` удаляются здесь же (удаления сигналов в коде нет — другого места чистки нет)."""
    conn.execute("DELETE FROM main.sub_keyword_hit WHERE ts < ?", (now_ms - KEYWORD_HIT_KEEP_MS,))
    conn.execute(
        "INSERT OR IGNORE INTO main.sub_keyword_hit (sub_kind, sub_id, message_id, ts) VALUES (?, ?, ?, ?)",
        (sub_kind, sub_id, message_id, now_ms),
    )


class MessageCat(NamedTuple):
    is_bot: bool
    cat: str  # `dm` | `mention` | `thread` | `other`
    thread_ext: str | None


def message_cat(conn, message_id: int) -> MessageCat:
    """Ф20-1 C2/D: категория входящего — `dm` (личка или групповая личка Slack) > `mention` (личное
    упоминание, `threads.direct_mention_sql`; у почты — мой адрес в «Кому», П9) > `thread` (настоящий ответ,
    не broadcast, в треде, на который владелец подписан, — как в `attention.for_me_sql`; или ответ на сообщение
    владельца `reply_to_ext_id`) > `other`. `thread_ext` — родитель треда у настоящего ответа, только формат `ts`
    Slack. Зовётся после строки треда (П2)."""
    schema = db_of(message_id)
    real_reply = threads_mod.real_reply_sql("m.thread_ext_id", "m.ext_id")
    row = conn.execute(
        f"SELECT m.is_bot, {threads_mod.direct_mention_sql('m')}, "  # noqa: S608 — доверенные фрагменты
        f"{real_reply}, {real_reply} AND m.thread_broadcast = 0 AND COALESCE(t.subscribed, 0) = 1, "
        f"m.reply_to_ext_id IS NOT NULL AND EXISTS (SELECT 1 FROM {schema}.message r "
        "WHERE r.channel_id = m.channel_id AND r.ext_id = m.reply_to_ext_id AND r.is_me = 1), "
        "m.thread_ext_id, c.kind, json_extract(c.meta_json, '$.attrs.slack_type') "
        f"FROM {schema}.message m JOIN {schema}.channel c ON c.id = m.channel_id "
        f"LEFT JOIN {schema}.thread t ON t.channel_id = m.channel_id AND t.ext_id = m.thread_ext_id "
        "WHERE m.id = ?",
        (message_id,),
    ).fetchone()
    if row is None:
        return MessageCat(False, "other", None)
    is_bot, direct, is_reply, in_my_thread, reply_to_me, thread_ext_id, kind, slack_type = row
    if kind == "dm" or slack_type == "mpim":
        cat = "dm"
    elif direct:
        cat = "mention"
    elif in_my_thread or reply_to_me:
        cat = "thread"
    else:
        cat = "other"
    thread_ext = (
        thread_ext_id
        if is_reply and isinstance(thread_ext_id, str) and THREAD_EXT_RE.fullmatch(thread_ext_id)
        else None
    )
    return MessageCat(bool(is_bot), cat, thread_ext)


def signal_for_message(
    conn, channel_id: int, message_id: int, *, ts: int, now_ms: int, rule_wake: tuple = ()
) -> bool:
    """A3: приём вставил НОВОЕ входящее (`is_me = 0`, не скрытое, не служебное) — в той же транзакции, ПОСЛЕ
    строки треда (Ф20-1 П2). Живая подписка беседы -> её `pending` (П12/П19: не старше подписки с запасом 10
    мин; боты — тоже, A6). П20 + Ф26-1 A4: беседа проекта, не в архиве, проект не заглушён — КАЖДОЙ живой
    подписке проекта `messages` по её окну, фильтру, ключам и заглушке беседы -> `pending` на (подписка,
    беседа); Ф20-1 A6/C2/П6: бот (`all` и `personal`) и не личное (`personal`) — строка `skipped` с причиной
    `bot`/`not_personal` вместо сигнала. У сигнала — категория (D). Ф25-1 A4 + П1: у «лично мне» с ключами
    чужое `other` сигналит только с ключом и без минус-слова (`keyword`, иначе `skipped` `no_keyword`); такая
    подписка слушает и заглушённые беседы проекта — сообщения не старше `channel.first_seen_ts`.

    Ф26-1 A4 + П7: подписка на беседу глушит подписку на проект только ТОЙ ЖЕ сессии; окно подписки на беседу
    разбор не обрывает — подписки других сессий проверяются дальше. Категория и текст — один раз на
    сообщение. `True` — хоть один сигнал вставлен или дописан.

    Ф31-1 решение 4 + П1: подписка на беседу с ключами — `other` только с ключом и без минус-слова
    (`keyword`, иначе `skipped` `no_keyword`), личное (`dm`/`mention`/`thread`) — всегда. Решение 2: каждый
    сигнал `keyword` оставляет пометку `sub_keyword_hit`.

    Ф39 A2 п.3: живых подписок беседы может быть несколько (своя у каждой сессии) — сигнал КАЖДОЙ, по её
    окну и её ключам; подписка на проект молчит, если её сессия держит любую из них.

    Ф63-1 П4: `rule_wake` — совпавшие правила входящих с `wake` (`inbound_rules.WakeRule`: номер правила,
    проект, ключи ролей). Подписка проекта правила в режиме «сообщения» с ролью из `wake` получает сигнал
    сразу (без пачки `batch_min`), мимо фильтра «лично мне», отсева бота и заглушки беседы, по своему окну и
    фильтру аккаунтов; заглушённый проект не будит. Сессия, слушающая беседу подпиской на беседу, получает
    его через неё (та главнее). У сигнала — номера правил (`rules_json`)."""
    return _signal(conn, channel_id, message_id, ts=ts, now_ms=now_ms, recheck=False, rule_wake=rule_wake)


def recheck_keywords(conn, message_id: int, *, now_ms: int) -> bool:
    """Ф31-1 решение 1 + П1, П4: перепроверка ключей после готовой расшифровки голосового или настоящей правки
    (в той же транзакции, после записи текста). Те же подписки и окна, что `signal_for_message`, но только
    сигнал `keyword` и только подпискам с ключами, для сообщения категории `other`; чужое, не скрытое, не
    удалённое, не служебное и не старше `RECHECK_MAX_AGE_MS`. Подписка, уже получившая сигнал «по ключу» об
    этом сообщении (`sub_keyword_hit`), молчит; ключа нет — тишина (строк `skipped` перепроверка не пишет).
    `True` — хоть один сигнал вставлен или дописан."""
    schema = db_of(message_id)
    row = conn.execute(
        f"SELECT channel_id, ts, is_me, hidden, deleted, purged, kind FROM {schema}.message "  # noqa: S608
        "WHERE id = ?",
        (message_id,),
    ).fetchone()
    if row is None:
        return False
    channel_id, ts, is_me, hidden, deleted, purged, kind = row
    if is_me or hidden or deleted or purged or kind == "event":
        return False
    if not isinstance(ts, int) or now_ms - ts > RECHECK_MAX_AGE_MS:
        return False
    return _signal(conn, channel_id, message_id, ts=ts, now_ms=now_ms, recheck=True)


def _woken(rule_wake: tuple, project_id: int | None, role_key: str | None) -> list[int]:
    """Ф63-1 П4: номера правил, будящих роль `role_key` в проекте `project_id` (по возрастанию)."""
    if not rule_wake or project_id is None or role_key is None:
        return []
    return sorted({w.rule_id for w in rule_wake if w.project_id == project_id and role_key in w.roles})


def _session_role_key(conn, key: tuple, project_id: int | None) -> str | None:
    """Ключ роли живой подписки на проект `project_id` той же сессии (`key` — токен, pid, starttime,
    разговор), режим «сообщения» — для правила с `wake` у подписки на беседу."""
    if project_id is None:
        return None
    row = conn.execute(
        "SELECT role_key FROM main.session_sub WHERE ended_ts IS NULL AND project_id = ? "
        "AND api_token_id = ? AND session_pid = ? AND session_proc_start = ? AND session_id = ? "
        "AND mode = 'messages' AND direct = 0 AND role_key IS NOT NULL ORDER BY id LIMIT 1",
        (project_id, *key),
    ).fetchone()
    return row[0] if row is not None else None


def _signal(
    conn, channel_id: int, message_id: int, *, ts: int, now_ms: int, recheck: bool, rule_wake: tuple = ()
) -> bool:
    """Общий разбор `signal_for_message` (`recheck=False`) и `recheck_keywords` (`recheck=True`)."""
    if recheck:
        rule_wake = ()  # Ф63-1 П2: правка и расшифровка правила не пересчитывают
    chans = live_subs_of_channel(conn, channel_id)  # Ф39 A2 п.3: сигнал — каждой живой подписке беседы
    schema = db_of(channel_id)
    channel = conn.execute(
        f"SELECT c.project_id, c.archived, c.muted, COALESCE(p.muted, 0), c.first_seen_ts, "  # noqa: S608
        f"c.account_id "
        f"FROM {schema}.channel c LEFT JOIN main.project p ON p.id = c.project_id WHERE c.id = ?",
        (channel_id,),
    ).fetchone()
    if channel is None or channel[1]:
        return False
    info: MessageCat | None = None
    text_cache: list[str | None] = []

    def _text() -> str | None:
        if not text_cache:
            text_cache.append(_message_text(conn, message_id))
        return text_cache[0]

    def _keyword(sub_kind: str, sub_id: int, compiled) -> list[str] | None:
        """Совпавшие ключи; при перепроверке ключ этой подписки по сообщению уже срабатывал — `None`."""
        if recheck and _hit_seen(conn, sub_kind, sub_id, message_id):
            return None
        return keywords_mod.check(compiled, _text())

    put = False
    for chan in chans:
        if ts < chan.created_ts - SIGNAL_TS_SLACK_MS:
            continue  # окно — своё у каждой подписки
        if info is None:
            info = message_cat(conn, message_id)  # категория и текст — один раз на сообщение
        rules = (
            _woken(rule_wake, channel[0], _session_role_key(conn, rec_key(chan), channel[0]))
            if rule_wake and channel[0] is not None and not channel[3]
            else []
        )
        if _signal_chan(
            conn,
            chan,
            channel_id,
            message_id,
            ts=ts,
            now_ms=now_ms,
            recheck=recheck,
            info=info,
            text=_text() if keywords_mod.has_keys(chan.keywords_json) else None,
            rules=rules,
        ):
            put = True
    # Ф39 A2 п.3: подписка на проект пропускается, если её сессия держит ЛЮБУЮ живую подписку беседы (не
    # глядя на окно — как было у одной).
    chan_keys = {rec_key(chan) for chan in chans}
    project_id, _archived, channel_muted, project_muted, first_seen_ts, account_id = channel
    if project_id is None:
        # Ф52-2 П2: беседа без проекта — подпискам КАЖДОГО незаглушённого проекта, слышащего «Неразобранное»
        # её аккаунта; дальше те же правила, что у беседы проекта.
        targets = [pid for pid, muted in unsorted_mod.listening_projects(conn, account_id) if not muted]
    else:
        targets = [] if project_muted else [project_id]
    if not targets:
        return put
    # Ф25-1 A4: подписки проекта — ДО проверки заглушки беседы: подписка с ключами слушает и заглушённые.
    marks = ",".join("?" for _ in targets)
    project_subs = conn.execute(
        "SELECT project_id, role_key, id, created_ts, msg_filter, keywords_json, accounts_json, "  # noqa: S608
        "api_token_id, session_pid, session_proc_start, session_id FROM main.session_sub "
        f"WHERE project_id IN ({marks}) AND ended_ts IS NULL AND mode = 'messages' AND direct = 0 "
        "ORDER BY id",
        targets,
    ).fetchall()
    for row in project_subs:
        sub_project, sub_role_key, sub_id, created_ts, msg_filter, keywords_json, accounts_json, *key = row
        if project_id is None and not unsorted_mod.token_sees(conn, key[0]):
            # Ревью Ф52-2: токен подписки сам «Неразобранное» не видит — для неё беседы нет: ни сигнала, ни
            # строки отсева, ни совпавших ключей.
            continue
        if not sub_accounts.listens(sub_accounts.parse(accounts_json), account_id):
            # Ф38-1 п.2–3: аккаунт беседы вне фильтра подписки — для неё беседы как бы нет: ни сигнала, ни
            # строки отсева (`skipped` значило бы «сессия получила и воздержалась»).
            continue
        if tuple(key) in chan_keys:
            continue  # П1: беседу эта сессия слушает подпиской на беседу — та главнее
        if ts < created_ts - SIGNAL_TS_SLACK_MS:
            continue
        compiled = (
            keywords_mod.compile(keywords_mod.parse_spec(keywords_json)) if msg_filter == "personal" else None
        )
        if recheck and compiled is None:
            continue  # перепроверка — только подпискам с ключами
        woken = _woken(rule_wake, sub_project, sub_role_key)
        if woken:
            # Ф63-1 П4: правило будит роль — сразу, мимо пачки, «лично мне», отсева бота и заглушки беседы.
            if info is None:
                info = message_cat(conn, message_id)
            keys = None
            if compiled is not None and info.cat == "other":
                keys = _keyword("project", sub_id, compiled)
            _put(
                conn,
                chan_sub_id=None,
                project_sub_id=sub_id,
                channel_id=channel_id,
                message_id=message_id,
                ts=ts,
                now_ms=now_ms,
                cat="keyword" if info.cat == "other" else info.cat,
                thread_ext=info.thread_ext,
                keys=keys,
                rules=woken,
            )
            if compiled is not None:
                _hit_add(conn, "project", sub_id, message_id, now_ms=now_ms)
            put = True
            continue
        if channel_muted and (compiled is None or first_seen_ts is None or ts < first_seen_ts):
            # Заглушённая беседа — только подписке с ключами и только сообщения не старше самой беседы в хабе
            # (П1: история, подгруженная при вступлении, не сигналит).
            continue
        if info is None:
            info = message_cat(conn, message_id)
        reason = None
        keys = None
        if info.is_bot:
            reason = "bot"
        elif msg_filter == "personal" and info.cat == "other":
            if compiled is None:
                reason = "not_personal"
            else:
                keys = _keyword("project", sub_id, compiled)
                if keys is None:
                    reason = "no_keyword"
        elif recheck:
            continue  # личное уже сигналило при приёме
        if reason is not None:
            if not recheck:
                _put_filtered(
                    conn,
                    project_sub_id=sub_id,
                    channel_id=channel_id,
                    message_id=message_id,
                    ts=ts,
                    reason=reason,
                    now_ms=now_ms,
                )
            continue
        _put(
            conn,
            chan_sub_id=None,
            project_sub_id=sub_id,
            channel_id=channel_id,
            message_id=message_id,
            ts=ts,
            now_ms=now_ms,
            cat="keyword" if keys else info.cat,
            thread_ext=info.thread_ext,  # П4: ключ в треде Slack — подсказка ведёт в `hub_thread`
            keys=keys,
            recheck=recheck,
            batch_ms=keywords_mod.batch_ms(keywords_json),  # Ф36-1 B2, Ф67-1: интервал любой подписки
        )
        if keys or compiled is not None:
            _hit_add(conn, "project", sub_id, message_id, now_ms=now_ms)  # ревью Ф31-1: и личный сигнал
        put = True
    return put


def _signal_chan(
    conn,
    chan: ChanSubRec,
    channel_id: int,
    message_id: int,
    *,
    ts: int,
    now_ms: int,
    recheck: bool,
    info: MessageCat,
    text: str | None,
    rules: list[int] | None = None,
) -> bool:
    """Сигнал подписке на беседу об одном сообщении — правила Ф31-1 (решение 4 + П1): общая часть разбора
    `_signal` и добора окна при возврате подписки по роли (Ф32-1 П2б). Окно подписки проверяет вызывающий;
    `text` — текст для ключей (нужен только подписке с ключами). `True` — сигнал вставлен или дописан.

    Ф63-1 П4: `rules` — правила входящих, будящие роль сессии этой подписки: сигнал есть всегда, сразу (без
    пачки), с номерами правил; чужое без ключа — категория «по ключу»."""
    spec = keywords_mod.parse_spec(chan.keywords_json)
    compiled = keywords_mod.compile(spec) if spec is not None else None
    cat, keys = info.cat, None
    if compiled is not None and info.cat == "other":
        # При перепроверке ключ этой подписки по сообщению уже срабатывал — молчим.
        if not (recheck and _hit_seen(conn, "channel", chan.id, message_id)):
            keys = keywords_mod.check(compiled, text)
        if keys is not None:
            cat = "keyword"
    if recheck:
        emit = keys is not None
    else:
        emit = compiled is None or info.cat != "other" or keys is not None or bool(rules)
    if rules and cat == "other":
        cat = "keyword"
    if emit:
        _put(
            conn,
            chan_sub_id=chan.id,
            project_sub_id=None,
            channel_id=channel_id,
            message_id=message_id,
            ts=ts,
            now_ms=now_ms,
            cat=cat,
            thread_ext=info.thread_ext,
            keys=keys,
            recheck=recheck,
            batch_ms=keywords_mod.batch_ms(chan.keywords_json) if not rules else 0,  # Ф36-1 B2, Ф67-1
            rules=rules,
        )
        if keys is not None or compiled is not None:
            # Ревью Ф31-1: подписке с ключами пометка — на ЛЮБОЙ выданный сигнал о сообщении (и личный):
            # правка, снявшая упоминание (`mention` -> `other`), не будит второй раз.
            _hit_add(conn, "channel", chan.id, message_id, now_ms=now_ms)
        return True
    if not recheck:
        _put_filtered(
            conn,
            chan_sub_id=chan.id,
            channel_id=channel_id,
            message_id=message_id,
            ts=ts,
            reason="no_keyword",
            now_ms=now_ms,
        )
    return False


def keys_text(text: str | None, transcripts: list) -> str | None:
    """Ф31-1 решение 3: текст для ключей — текст сообщения и тексты готовых расшифровок его вложений (через
    перевод строки). Одна функция для сигнала (`_message_text`) и пометки в чтении (`message_keys_text`)."""
    parts = [part for part in (text, *transcripts) if isinstance(part, str) and part]
    return "\n".join(parts) if parts else None


def _message_text(conn, message_id: int) -> str | None:
    schema = db_of(message_id)
    row = conn.execute(f"SELECT text FROM {schema}.message WHERE id = ?", (message_id,)).fetchone()  # noqa: S608
    if row is None:
        return None
    done = conn.execute(
        f"SELECT t.text FROM {schema}.transcript t JOIN {schema}.media md ON md.id = t.media_id "  # noqa: S608
        "WHERE md.message_id = ? AND t.status = 'done' ORDER BY md.idx",
        (message_id,),
    ).fetchall()
    return keys_text(row[0], [r[0] for r in done])


def message_keys_text(message: dict) -> str | None:
    """Текст для ключей у сообщения выдачи (`media[].transcript` кладёт `media.attach_media`)."""
    done = [
        item["transcript"].get("text")
        for item in message.get("media") or []
        if isinstance(item, dict)
        and isinstance(item.get("transcript"), dict)
        and item["transcript"].get("status") == "done"
    ]
    return keys_text(message.get("text"), done)


def skip_project_signals(conn, channel_ids: list[int], *, reason: str, now_ms: int) -> int:
    """Ф17-1 П4: беседу заглушили привязкой после вступления — ещё не выданные сигналы подписке на ПРОЕКТ по
    ней (и её темам) снимаются `skipped` с причиной: сообщения, успевшие прийти до заглушки, не будят. Сигналы
    подписок на саму беседу не трогаются. Число снятых."""
    if not channel_ids:
        return 0
    placeholders = ",".join("?" for _ in channel_ids)
    cur = conn.execute(
        "UPDATE main.session_chan_signal SET status = 'skipped', reason = ?, updated_ts = ? "  # noqa: S608
        f"WHERE channel_id IN ({placeholders}) AND project_sub_id IS NOT NULL AND status = 'pending'",
        (reason, now_ms, *channel_ids),
    )
    return cur.rowcount


def _answered(conn, channel_id: int, last_message_id: int) -> bool:
    """П2: ровно `agent_items.ANSWERED_SQL`, `lm` — последнее сообщение сигнала, беседа — нынешняя беседа
    сигнала (после миграции беседы — новая, П18)."""
    schema = db_of(last_message_id)
    row = conn.execute(
        agent_items_mod._sql(  # noqa: SLF001 — тот же приём подстановки схемы
            schema,
            "SELECT " + agent_items_mod.ANSWERED_SQL + " FROM {schema}.message lm "  # noqa: S608
            "JOIN (SELECT ? AS channel_id) a WHERE lm.id = ?",
        ),
        (channel_id, last_message_id),
    ).fetchone()
    return bool(row[0]) if row is not None else False


def on_channel_migrate(conn, old_id: int, new_id: int, *, now_ms: int) -> None:
    """П11 + П18: группа -> супергруппа, письма трекера. Живая подписка старой беседы переходит на новую
    (`channel_id` и у `pending`); у новой уже есть своя живая — старая завершается `channel_gone`. `pending`
    подписки на проект (П20) — тоже на новую беседу (там уже есть свой — старый `ended`).

    Ф39 A2 п.5: по КАЖДОЙ живой подписке старой беседы; «своя живая на новой» — у той же сессии."""
    for old in live_subs_of_channel(conn, old_id):
        if live_of_session(conn, new_id, rec_key(old)) is not None:
            end(conn, old.id, "channel_gone", now_ms=now_ms, actor="core")
        else:
            conn.execute("UPDATE main.session_chan_sub SET channel_id = ? WHERE id = ?", (new_id, old.id))
            conn.execute(
                "UPDATE main.session_chan_signal SET channel_id = ?, updated_ts = ? "
                "WHERE chan_sub_id = ? AND status = 'pending'",
                (new_id, now_ms, old.id),
            )
    for signal_id, project_sub_id in conn.execute(
        "SELECT id, project_sub_id FROM main.session_chan_signal "
        "WHERE channel_id = ? AND project_sub_id IS NOT NULL AND status = 'pending'",
        (old_id,),
    ).fetchall():
        taken = conn.execute(
            "SELECT 1 FROM main.session_chan_signal "
            "WHERE project_sub_id = ? AND channel_id = ? AND status = 'pending'",
            (project_sub_id, new_id),
        ).fetchone()
        if taken is not None:
            conn.execute(
                "UPDATE main.session_chan_signal SET status = 'ended', updated_ts = ? WHERE id = ?",
                (now_ms, signal_id),
            )
        else:
            conn.execute(
                "UPDATE main.session_chan_signal SET channel_id = ?, updated_ts = ? WHERE id = ?",
                (new_id, now_ms, signal_id),
            )


def _alive(
    conn,
    sub_kind: str,
    sub_id: int,
    channel_id: int,
    *,
    created_ts: int,
    msg_count: int,
    first_id: int,
    last_id: int,
) -> tuple[int, int]:
    """Ф36-1 C + П2, П3: сколько сообщений ждущей строки сигнала живо к моменту выдачи и какое из них назвать
    в ответе — `(число живых, message_id)`; число 0 — строку снять `skipped` `deleted`.

    C1 + П2: у подписки есть пометки `sub_keyword_hit` этой строки (поставлены не раньше её `created_ts`, по
    сообщениям этой беседы) — считаются МЁРТВЫЕ (`deleted`/`hidden`/`purged`): пометок может быть меньше, чем
    сообщений в строке (ключи поставлены живой подписке на месте, чистка пометок, перенос беседы). Мёртвых не
    меньше `msg_count` — 0; иначе `msg_count - мёртвых`, а `message_id` — последнее сообщение строки, если оно
    живо, иначе самое позднее живое из помеченных по времени источника (помеченных живых нет — первое
    сообщение строки).
    C2: пометок нет — снимается только строка из одного сообщения, и только если оно удалено; иначе строка
    выдаётся как есть (без пометок пересчёта нет)."""
    schema = db_of(channel_id)
    rows = conn.execute(
        f"SELECT m.id, m.ts, m.deleted OR m.hidden OR m.purged FROM main.sub_keyword_hit h "  # noqa: S608
        f"JOIN {schema}.message m ON m.id = h.message_id "
        "WHERE h.sub_kind = ? AND h.sub_id = ? AND h.ts >= ? AND m.channel_id = ?",
        (sub_kind, sub_id, created_ts, channel_id),
    ).fetchall()
    if not rows:
        if msg_count == 1:
            gone = conn.execute(
                f"SELECT deleted OR hidden OR purged FROM {schema}.message WHERE id = ?",  # noqa: S608
                (first_id,),
            ).fetchone()
            if gone is not None and gone[0]:
                return 0, last_id
        return msg_count, last_id
    dead = {message_id for message_id, _ts, gone in rows if gone}
    live = [(ts, message_id) for message_id, ts, gone in rows if not gone]
    # Ревью: пометок бывает и БОЛЬШЕ, чем сообщений в строке (перепроверка ключа по сообщению внутри диапазона
    # ставит пометку, не увеличивая число), — живая пометка значит живое сообщение, строку не снимать.
    if not live and len(dead) >= msg_count:
        return 0, last_id
    if last_id not in dead:
        newest = last_id
    elif live:
        newest = max(live)[1]
    else:
        newest = first_id if first_id not in dead else last_id  # живые — без пометок: первое сообщение строки
    return max(msg_count - len(dead), len(live), 1), newest


def _skip_deleted(conn, signal_id: int, *, now_ms: int) -> None:
    """Ф36-1 C + П3: в строке меняется только статус и причина (`last_message_id` не переписывается)."""
    conn.execute(
        "UPDATE main.session_chan_signal SET status = 'skipped', reason = ?, updated_ts = ? WHERE id = ?",
        (DELETED_REASON, now_ms, signal_id),
    )


def carry_project_signals(
    conn,
    source,
    new_sub_id: int,
    *,
    batch_ms: int,
    now_ms: int,
    accounts_json: str | None = None,
    same_held: bool = False,
) -> int:
    """Ф36-1 П1: новая подписка на проект в режиме `messages` забирает ждущие сигналы ЗАВЕРШЁННОГО источника
    наследования (`source` — `subs.SubRec`), если тот завершён не раньше `CARRY_MAX_AGE_MS` назад: строки
    источника, закрытые вместе с ним (`ended`, `updated_ts` = его `ended_ts`), — снова `pending`, уже новой
    подписки; `created_ts` и `ready_ts` — как были. Пометки `sub_keyword_hit` копируются с исходным `ts`
    (правка сообщения, о котором источник уже получил сигнал, новую не будит; пересчёт удалённых работает).
    Зовётся сразу после вставки новой подписки — своих `pending` у неё ещё нет. Число перенесённых.

    Ф38-1 п.9: `accounts_json` — фильтр НОВОЙ подписки по аккаунтам: сигналы бесед, которых она не слышит, не
    переезжают (остаются у завершённого источника как есть).

    Ф41-2а П7: `same_held` — источник той же сессии хаба (та же условная тройка, сменился токен): переезжают
    и сигналы подписки, снятой с `token_revoked`."""
    spec = sub_accounts.parse(accounts_json)
    if source.ended_ts is None or now_ms - source.ended_ts > CARRY_MAX_AGE_MS:
        return 0
    if source.end_reason not in CARRY_REASONS and not (
        same_held and source.end_reason in SAME_HELD_REASONS
    ):
        return 0  # ревью: явная отписка, снятие владельцем, отзыв токена — насовсем, как у возврата бесед
    rows = conn.execute(
        "SELECT id, channel_id, ready_ts FROM main.session_chan_signal WHERE project_sub_id = ? "
        "AND status = 'ended' AND updated_ts = ? ORDER BY id DESC",
        (source.id, source.ended_ts),
    ).fetchall()
    taken: set[int] = set()
    for signal_id, channel_id, ready_ts in rows:
        if channel_id in taken:
            continue  # один `pending` на (подписка, беседа): из закрытых в тот же миг — самая свежая
        if spec is not None and not sub_accounts.listens(spec, subs.channel_account(conn, channel_id)):
            continue
        taken.add(channel_id)
        # Ревью: срок — по правилам НОВОЙ подписки (`batch_ms` — её интервал): пачки у неё нет — строка
        # готова сразу; есть — не позже ближайшего узла её сетки. Готовая (`NULL`) готовой и остаётся.
        if ready_ts is not None:
            ready_ts = min(ready_ts, batch_ready_ts(now_ms, batch_ms)) if batch_ms > 0 else None
        conn.execute(
            "UPDATE main.session_chan_signal SET project_sub_id = ?, status = 'pending', updated_ts = ?, "
            "ready_ts = ? WHERE id = ?",
            (new_sub_id, now_ms, ready_ts, signal_id),
        )
    conn.execute(
        "INSERT OR IGNORE INTO main.sub_keyword_hit (sub_kind, sub_id, message_id, ts) "
        "SELECT 'project', ?, message_id, ts FROM main.sub_keyword_hit "
        "WHERE sub_kind = 'project' AND sub_id = ?",
        (new_sub_id, source.id),
    )
    return len(taken)


# --- ленивые проверки и выдача (A2, П8, П10, П13, П16) --------------------------------------------------


def lazy_actions(
    conn, recs: list[ChanSubRec], project_ids: frozenset[int] | None
) -> list[tuple[int, str, object]]:
    """Только чтение: что сделать с подписками перед выдачей. `(sub_id, "end", причина)` — беседа в архиве или
    удалена (`channel_gone`), перенесена в другой проект или перестала быть видна токену (`moved`);
    `(sub_id, "adopt", project_id)` — беседу разложили из «Неразобранного» в проект (П13: подписка жива)."""
    actions: list[tuple[int, str, object]] = []
    for rec in recs:
        channel = _channel(conn, rec.channel_id)
        if channel is None or channel.archived:
            actions.append((rec.id, "end", "channel_gone"))
        elif not visible(project_ids, channel.project_id):
            actions.append((rec.id, "end", "moved"))
        elif rec.project_id is None:
            if channel.project_id is not None:
                actions.append((rec.id, "adopt", channel.project_id))
        elif channel.project_id != rec.project_id:
            actions.append((rec.id, "end", "moved"))
    return actions


def apply_actions(
    conn,
    actions: list[tuple[int, str, object]],
    *,
    now_ms: int,
    actor: str,
    api_token_id: int | None,
    client_name: str | None,
    request_id: str,
) -> None:
    for sub_id, what, value in actions:
        if what == "adopt":
            conn.execute(
                "UPDATE main.session_chan_sub SET project_id = ? "
                "WHERE id = ? AND ended_ts IS NULL AND project_id IS NULL",
                (value, sub_id),
            )
        else:
            end(
                conn,
                sub_id,
                str(value),
                now_ms=now_ms,
                actor=actor,
                api_token_id=api_token_id,
                client_name=client_name,
                request_id=request_id,
            )


def session_subs(conn, token_id: int, pid: int, proc_start: int, session_id: str) -> list[ChanSubRec]:
    """П5/П17: живые подписки ЭТОГО разговора (токен, процесс, `session_id`)."""
    return [r for r in live_of_process(conn, pid, proc_start, token_id) if r.session_id == session_id]


def debounce_left_ms(recs: list[ChanSubRec], now_ms: int) -> int:
    """Дебаунс 5 с от последней выдачи сессии (`max(last_delivery_ts)` её подписок); первая — сразу."""
    last = max((r.last_delivery_ts for r in recs if r.last_delivery_ts is not None), default=None)
    if last is None:
        return 0
    elapsed = now_ms - last
    if elapsed < 0 or elapsed >= CHAN_DEBOUNCE_MS:
        return 0
    return CHAN_DEBOUNCE_MS - elapsed


def touch_wait(conn, sub_ids: list[int], *, now_ms: int) -> None:
    for sub_id in sub_ids:
        conn.execute(
            "UPDATE main.session_chan_sub SET last_wait_ts = ? WHERE id = ? AND ended_ts IS NULL",
            (now_ms, sub_id),
        )


def deliver(
    conn,
    *,
    token_id: int,
    pid: int,
    proc_start: int,
    session_id: str,
    project_ids: frozenset[int] | None,
    now_ms: int,
    client_name: str | None,
    request_id: str,
) -> list[dict]:
    """Выдача одной транзакцией писателя по ВСЕМ живым подпискам разговора: ленивые проверки, затем каждый
    `pending`: владелец ответил после последнего сообщения сигнала — `skipped answered` (П3: прочитанность и
    присутствие не снимают), иначе `delivered` (не больше 20, старшие первыми; аудит `chan_sub_signal`).

    Ф36-1 B6 + П5: выдаются ВСЕ ждущие, не удержанные расшифровкой, не глядя на срок пачки (зовётся выдача,
    только когда есть готовое, — несозревшие «по ключу» уходят тем же выходом); готовые — первыми. C: строка,
    чьи сообщения к выдаче удалены, — `skipped` `deleted`; удалена часть — в ответе число живых и последнее
    живое (`_alive`).

    Ф39 A3: в каждом сигнале целое `others` — сколько ещё живых сессий слушает эту беседу (`listeners`)."""
    own_key = session_key(token_id, pid, proc_start, session_id)
    recs = session_subs(conn, token_id, pid, proc_start, session_id)
    actions = lazy_actions(conn, recs, project_ids)
    if actions:
        apply_actions(
            conn,
            actions,
            now_ms=now_ms,
            actor="api",
            api_token_id=token_id,
            client_name=client_name,
            request_id=request_id,
        )
        recs = session_subs(conn, token_id, pid, proc_start, session_id)
    if not recs:
        return []
    ids = [r.id for r in recs]
    placeholders = ",".join("?" for _ in ids)
    pending = conn.execute(
        "SELECT id, chan_sub_id, channel_id, last_message_id, msg_count, first_message_id, cat, "  # noqa: S608
        "thread_ext, keys_json, created_ts, rules_json FROM main.session_chan_signal "
        f"WHERE status = 'pending' AND chan_sub_id IN ({placeholders}) "
        "ORDER BY (ready_ts IS NOT NULL AND ready_ts > ?), created_ts, id",
        (*ids, now_ms),
    ).fetchall()
    delivered: list[dict] = []
    touched: set[int] = set()
    for row in pending:
        signal_id, sub_id, channel_id, last_id, msg_count, first_id, cat, thread_ext, keys_json = row[:9]
        if hold_ms(conn, channel_id, first_id, now_ms=now_ms) > 0:
            continue  # Ф14-6b П1: ждёт расшифровку — остаётся `pending`, ничего не пишется
        if _answered(conn, channel_id, last_id):
            conn.execute(
                "UPDATE main.session_chan_signal SET status = 'skipped', reason = 'answered', updated_ts = ? "
                "WHERE id = ?",
                (now_ms, signal_id),
            )
            continue
        msg_count, last_id = _alive(
            conn,
            "channel",
            sub_id,
            channel_id,
            created_ts=row[9],
            msg_count=msg_count,
            first_id=first_id,
            last_id=last_id,
        )
        if msg_count == 0:
            _skip_deleted(conn, signal_id, now_ms=now_ms)  # Ф36-1 C: удалено к моменту выдачи
            continue
        if len(delivered) >= MAX_SIGNALS_PER_DELIVERY:
            continue  # остаются `pending` — следующей выдачей
        channel = _channel(conn, channel_id)
        account_id = channel.account_id if channel is not None else 0
        keys = _load_keys(keys_json)
        conn.execute(
            "UPDATE main.session_chan_signal SET status = 'delivered', delivered_ts = ?, updated_ts = ? "
            "WHERE id = ?",
            (now_ms, now_ms, signal_id),
        )
        write_audit(
            conn,
            now_ms=now_ms,
            actor="api",
            api_token_id=token_id,
            client_name=client_name,
            request_id=request_id,
            action="chan_sub_signal",
            result="delivered",
            channel_id=channel_id,
            detail={
                "sub_id": sub_id,
                "account_id": account_id,
                "msg_count": msg_count,
                "message_id": last_id,
                "cat": cat or "other",
                **({"keys": len(keys)} if keys else {}),
            },
        )
        touched.add(sub_id)
        signal = {
            "sub_id": sub_id,
            "channel_id": channel_id,
            "account_id": account_id,
            "msg_count": msg_count,
            "message_id": last_id,
            # Ф20-1 D: категория (`NULL` старых строк — «сообщение») и родитель треда Slack.
            "cat": cat or "other",
            "thread_ext": thread_ext,
            "others": len(listeners(conn, channel_id, except_key=own_key)),  # Ф39 A3
        }
        if keys:
            signal["keys"] = keys  # Ф31-1 П6: совпавшие ключи подписки на беседу — данные самой сессии
        rule_ids = load_rules(row[10])
        if rule_ids:
            signal["rules"] = rule_ids  # Ф63-1 П4: номера правил входящих — только числа
        delivered.append(signal)
    for sub_id in touched:
        conn.execute(
            "UPDATE main.session_chan_sub SET last_delivery_ts = ?, last_wait_ts = ? WHERE id = ?",
            (now_ms, now_ms, sub_id),
        )
    return delivered


def _chan_sub_same_session(conn, channel_id: int, sub) -> bool:
    """Ф26-1 A3: у беседы живая подписка на беседу той же сессии, что держит подписку `sub` на проект."""
    key = session_key(sub.api_token_id, sub.session_pid, sub.session_proc_start, sub.session_id)
    return live_of_session(conn, channel_id, key) is not None


def _project_skip_reason(
    conn,
    sub,
    channel: _Channel | None,
    channel_id: int,
    last_id: int,
    visible_projects,
    api_token_id: int | None = None,
) -> str | None:
    """Годность сигнала сообщений подписки на проект при выдаче: причина пропуска или `None`. Одно
    определение для выдачи (`deliver_project_messages`) и счёта готового хосту (`valid_state`, Ф41-4 П2)."""
    if channel is None or channel.archived:
        return "closed"
    if channel.project_id is None:
        # Ф52-2 П2: беседа без проекта годна, пока проект подписки слышит «Неразобранное» её аккаунта (право
        # проверяется и при выдаче: снято — не выдаётся; беседу разложили — ветка ниже).
        if not subs.unsorted_hears(conn, sub, channel.account_id, visible_projects, api_token_id):
            return "moved"
    elif channel.project_id != sub.project_id or not visible(visible_projects, channel.project_id):
        return "moved"
    if _chan_sub_same_session(conn, channel_id, sub):
        return "chan_sub"
    if _answered(conn, channel_id, last_id):
        return "answered"
    return None


def valid_state(
    conn,
    *,
    now_ms: int,
    chan_sub_ids: list[int] | None = None,
    project_sub=None,
    visible_projects: frozenset[int] | None = None,
) -> tuple[int, int, int, int]:
    """Ф41-4 П2, читатель: как `ready_state`, но считает только ГОДНОЕ к выдаче — строки, которые выдача
    сняла бы (`answered`, `moved`, `closed`, `chan_sub`, удалённые), не считаются. `(готовых сейчас, ждущих
    срока пачки или расшифровки, мс до ближайшего срока — 0, если ждущих нет, сколько из готовых — личные)`.
    Либо подписки на беседы (`chan_sub_ids`), либо подписка на проект в режиме `messages` (`project_sub` —
    `subs.SubRec`).

    21.10: личное — категория `PERSONAL_CATS` и последнее сообщение сигнала не от бота; срочность подачи
    сессии хаба (`held_signals`), выдачу не меняет."""
    if project_sub is not None:
        where, params, kind = "project_sub_id = ?", [project_sub.id], "project"
    else:
        if not chan_sub_ids:
            return 0, 0, 0
        placeholders = ",".join("?" for _ in chan_sub_ids)
        where, params, kind = f"chan_sub_id IN ({placeholders})", list(chan_sub_ids), "channel"
    rows = conn.execute(
        "SELECT chan_sub_id, channel_id, last_message_id, msg_count, first_message_id, ready_ts, "  # noqa: S608
        f"created_ts, cat FROM main.session_chan_signal WHERE status = 'pending' AND {where}",
        params,
    ).fetchall()
    ready = waiting = nearest = personal = 0
    for chan_sub_id, channel_id, last_id, msg_count, first_id, ready_ts, created_ts, cat in rows:
        if project_sub is not None:
            channel = _channel(conn, channel_id)
            if _project_skip_reason(conn, project_sub, channel, channel_id, last_id, visible_projects):
                continue
            owner = project_sub.id
        else:
            if _answered(conn, channel_id, last_id):
                continue
            owner = chan_sub_id
        alive, _last = _alive(
            conn,
            kind,
            owner,
            channel_id,
            created_ts=created_ts,
            msg_count=msg_count,
            first_id=first_id,
            last_id=last_id,
        )
        if alive == 0:
            continue
        if ready_ts is not None and ready_ts > now_ms:
            held = ready_ts - now_ms
        else:
            held = hold_ms(conn, channel_id, first_id, now_ms=now_ms)
        if held <= 0:
            ready += 1
            if cat in PERSONAL_CATS and not _from_bot(conn, last_id):
                personal += 1
        else:
            waiting += 1
            if nearest == 0 or held < nearest:
                nearest = held
    return ready, waiting, nearest, personal


def _from_bot(conn, message_id: int | None) -> bool:
    if message_id is None:
        return False
    row = conn.execute(
        f"SELECT is_bot FROM {db_of(message_id)}.message WHERE id = ?",  # noqa: S608
        (message_id,),
    ).fetchone()
    return bool(row and row[0])


def deliver_project_messages(
    conn,
    sub,
    *,
    now_ms: int,
    visible_projects: frozenset[int] | None,
    api_token_id: int,
    client_name: str | None,
    request_id: str,
) -> list[dict]:
    """П20: выдача подписке на проект в режиме `messages` (из `subs.deliver`). Пропуск: `answered` (П2),
    беседа ушла из проекта или не видна токену — `moved`, в архиве — `closed`, у беседы появилась подписка на
    беседу — `chan_sub` (П1: она главнее). Прочитанность и присутствие не снимают (П3).

    Ф36-1 B6 + П5, C: как `deliver` — все ждущие, не глядя на срок пачки, готовые первыми; удалённые к выдаче
    — `skipped` `deleted` (`_alive`)."""
    pending = conn.execute(
        "SELECT id, channel_id, last_message_id, msg_count, first_message_id, cat, thread_ext, keys_json, "
        "created_ts, rules_json FROM main.session_chan_signal "
        "WHERE project_sub_id = ? AND status = 'pending' "
        "ORDER BY (ready_ts IS NOT NULL AND ready_ts > ?), created_ts, id",
        (sub.id, now_ms),
    ).fetchall()
    delivered: list[dict] = []
    for row in pending:
        signal_id, channel_id, last_id, msg_count, first_id, cat, thread_ext, keys_json, created_ts = row[:9]
        if hold_ms(conn, channel_id, first_id, now_ms=now_ms) > 0:
            continue  # Ф14-6b П1: ждёт расшифровку — остаётся `pending`, ничего не пишется
        channel = _channel(conn, channel_id)
        reason = _project_skip_reason(
            conn, sub, channel, channel_id, last_id, visible_projects, api_token_id
        )
        if reason is not None:
            conn.execute(
                "UPDATE main.session_chan_signal SET status = 'skipped', reason = ?, updated_ts = ? "
                "WHERE id = ?",
                (reason, now_ms, signal_id),
            )
            continue
        msg_count, last_id = _alive(
            conn,
            "project",
            sub.id,
            channel_id,
            created_ts=created_ts,
            msg_count=msg_count,
            first_id=first_id,
            last_id=last_id,
        )
        if msg_count == 0:
            _skip_deleted(conn, signal_id, now_ms=now_ms)  # Ф36-1 C: удалено к моменту выдачи
            continue
        if len(delivered) >= MAX_SIGNALS_PER_DELIVERY:
            continue
        keys = _load_keys(keys_json)
        conn.execute(
            "UPDATE main.session_chan_signal SET status = 'delivered', delivered_ts = ?, updated_ts = ? "
            "WHERE id = ?",
            (now_ms, now_ms, signal_id),
        )
        write_audit(
            conn,
            now_ms=now_ms,
            actor="api",
            api_token_id=api_token_id,
            client_name=client_name,
            request_id=request_id,
            action="sub_signal",
            result="delivered",
            channel_id=channel_id,
            detail={
                "sub_id": sub.id,
                "kind": "messages",
                "account_id": channel.account_id,
                "msg_count": msg_count,
                "message_id": last_id,
                "cat": cat or "other",
                **({"msg_filter": sub.msg_filter} if sub.msg_filter != "all" else {}),
                **({"keys": len(keys)} if keys else {}),
            },
        )
        signal = {
            "kind": "messages",
            "channel_id": channel_id,
            "account_id": channel.account_id,
            "msg_count": msg_count,
            "message_id": last_id,
            # Ф20-1 D: категория (`NULL` старых строк — «сообщение») и родитель треда Slack.
            "cat": cat or "other",
            "thread_ext": thread_ext,
        }
        if channel.project_id is None:
            signal["unsorted"] = True  # Ф52-2 П2: клиент пишет строку «неразобранное (аккаунт N): …»
        if keys:
            signal["keys"] = keys  # Ф25-1 A5: совпавшие ключи — данные самой сессии
        rule_ids = load_rules(row[9])
        if rule_ids:
            signal["rules"] = rule_ids  # Ф63-1 П4: номера правил входящих — только числа
        delivered.append(signal)
    if delivered:
        conn.execute(
            "UPDATE main.session_sub SET last_delivery_ts = ?, last_wait_ts = ? WHERE id = ?",
            (now_ms, now_ms, sub.id),
        )
    return delivered


# --- пометка `keys` в чтении (Ф25-1 A5, П3, П4) ----------------------------------------------------------


def annotate_keys(conn, channel_id: int, messages: list[dict]) -> None:
    """Ленты `GET /channels/{id}/messages`, `/wait`, `/threads/{ext}/messages` для bearer: у проекта беседы
    живая подписка «лично мне» с ключами — каждому чужому сообщению (`is_me` ложно) с совпавшим ключом без
    минус-слова поле `keys`. Ф26-1 A5: таких подписок может быть несколько — ключи объединяются (порядок
    первого появления, не больше `SIGNAL_KEYS_MAX`): чтение не знает сессию, совпадение — подсказка. Одна
    выборка подписок и одна раскладка ключей каждой на запрос.

    Ф31-1 решение 3 + П7: текст — с готовой расшифровкой голосового (`message_keys_text`); ключи живой
    подписки на эту беседу — тоже в объединении (Ф39 A2 п.7: ВСЕХ живых подписок беседы)."""
    if not messages:
        return
    channel = _channel(conn, channel_id)
    if channel is None:
        return
    raws: list[str] = [
        live.keywords_json
        for live in live_subs_of_channel(conn, channel_id)
        if keywords_mod.has_keys(live.keywords_json)
    ]
    if channel.project_id is not None:
        rows = conn.execute(
            "SELECT keywords_json FROM main.session_sub WHERE project_id = ? AND ended_ts IS NULL "
            "AND mode = 'messages' AND msg_filter = 'personal' AND keywords_json IS NOT NULL ORDER BY id",
            (channel.project_id,),
        ).fetchall()
        raws.extend(raw for (raw,) in rows)
    compiled = [c for raw in raws if (c := keywords_mod.compile(keywords_mod.parse_spec(raw))) is not None]
    if not compiled:
        return
    for message in messages:
        if not isinstance(message, dict) or message.get("is_me"):
            continue
        text = message_keys_text(message)
        if text is None:
            continue
        merged: list[str] = []
        for one in compiled:
            for key in keywords_mod.check(one, text) or []:
                if key not in merged:
                    merged.append(key)
        if merged:
            message["keys"] = merged[: keywords_mod.SIGNAL_KEYS_MAX]


# --- поле `msg_signal` в `GET /agent/items` (после ревью, п.4) ------------------------------------------


def _pick(regular: dict | None, skipped: dict | None) -> dict | None:
    """Ф20-1 П6 + после ревью: строка отфильтрованного берётся, если обычного сигнала нет или он уже
    выдан/снят и покрывает более раннее сообщение. Невыданный обычный сигнал отфильтрованным не
    перекрывается — иначе бот или не личное после личного `pending` давали бы дежурной «сессия получила» без
    запасного пути."""
    if skipped is not None and (
        regular is None
        or (regular["status"] != "pending" and skipped["last_message_id"] > regular["last_message_id"])
    ):
        return skipped
    return regular


def attach_msg_signals(conn, items: list[dict]) -> None:
    """У каждой карточки — последний сигнал СООБЩЕНИЙ по её беседе от живой подписки, которая беседу слушает:
    подписка на беседу (она главнее) или подписка на проект карточки в режиме `messages`; иначе `None`.
    `sub_ref` — `c<id>` у подписки на беседу, `<id>` у подписки на проект. По нему hub-wake решает, получила
    ли сессия последнее сообщение карточки (номеров карточек такая сессия не знает и закрыть их не может).

    Ф20-1 П6: у подписки на проект строка отфильтрованного (`skipped` `bot`/`not_personal`) берётся, если она
    покрывает более позднее сообщение, чем последний обычный сигнал: сообщение, отсеянное фильтром, — не
    работа дежурной («сессия получила»). Ф20-1 D: в сигнале — `cat` и `thread_ext`. Ф36-1 D: и `ready_ts`.

    Ф26-1 A5: `msg_signals` — по сигналу на КАЖДУЮ живую подписку, слушающую беседу (`sub_ref`: подписка на
    беседу и подписки проекта карточки в режиме `messages`), правило П6 — внутри каждой подписки; старое
    `msg_signal` — как было.

    Ф39 A2 п.8: живых подписок беседы может быть несколько — в `msg_signals` сигнал каждой (`c<id>`, по
    `id`), затем подписки проекта; `msg_signal` — сигнал живой подписки беседы с наименьшим `id`."""
    for item in items:
        item["msg_signal"] = None
        item["msg_signals"] = []
    channel_ids = sorted({item["channel_id"] for item in items if isinstance(item.get("channel_id"), int)})
    if not channel_ids:
        return
    live_chan: dict[int, list[int]] = {}  # беседа -> живые подписки беседы, по `id`
    by_chan: dict[tuple[int, int], dict] = {}  # ключ (беседа, подписка на беседу)
    filtered_chan: dict[tuple[int, int], dict] = {}
    by_project: dict[tuple[int, int], dict] = {}
    filtered: dict[tuple[int, int], dict] = {}
    # Ф26-1: то же по каждой подписке на проект — ключ (беседа, проект, подписка).
    by_sub: dict[tuple[int, int, int], dict] = {}
    filtered_by_sub: dict[tuple[int, int, int], dict] = {}
    subs_of: dict[tuple[int, int], set[int]] = {}
    for start in range(0, len(channel_ids), 500):
        chunk = channel_ids[start : start + 500]
        marks = ",".join("?" for _ in chunk)
        for cid, chan_sub_id in conn.execute(
            "SELECT channel_id, id FROM main.session_chan_sub "  # noqa: S608
            f"WHERE ended_ts IS NULL AND channel_id IN ({marks}) ORDER BY id",
            chunk,
        ):
            live_chan.setdefault(cid, []).append(chan_sub_id)
        rows = conn.execute(
            "SELECT s.channel_id, s.chan_sub_id, s.project_sub_id, u.project_id, s.status, s.reason, "  # noqa: S608
            "s.delivered_ts, s.created_ts, s.last_message_id, s.cat, s.thread_ext, s.ready_ts "
            "FROM main.session_chan_signal s "
            "LEFT JOIN main.session_chan_sub cs ON cs.id = s.chan_sub_id AND cs.ended_ts IS NULL "
            "AND cs.channel_id = s.channel_id "
            "LEFT JOIN main.session_sub u ON u.id = s.project_sub_id AND u.ended_ts IS NULL "
            "AND u.mode = 'messages' "
            f"WHERE s.channel_id IN ({marks}) AND (cs.id IS NOT NULL OR u.id IS NOT NULL) ORDER BY s.id",
            chunk,
        ).fetchall()
        for row in rows:
            cid, chan_sub_id, project_sub_id, project_id, status, reason, delivered_ts, created = row[:8]
            last = row[8]
            signal = {
                "sub_ref": f"c{chan_sub_id}" if chan_sub_id is not None else str(project_sub_id),
                "status": status,
                "reason": reason,
                "delivered_ts": delivered_ts,
                "created_ts": created,
                "last_message_id": last,
                "cat": row[9],
                "thread_ext": row[10],
                "ready_ts": row[11],  # Ф36-1 D: срок пачки (дежурной больше нет)
            }
            if chan_sub_id is not None:
                # Ф31-1 П2: у подписки на беседу с ключами — тоже строка отфильтрованного (`no_keyword`),
                # выбор — как у подписки на проект (`_pick`).
                if status == "skipped" and reason in FILTER_REASONS:
                    prev = filtered_chan.get((cid, chan_sub_id))
                    if prev is None or last >= prev["last_message_id"]:
                        filtered_chan[(cid, chan_sub_id)] = signal
                else:
                    by_chan[(cid, chan_sub_id)] = signal
                continue
            subs_of.setdefault((cid, project_id), set()).add(project_sub_id)
            if status == "skipped" and reason in FILTER_REASONS:
                prev = filtered.get((cid, project_id))
                if prev is None or last >= prev["last_message_id"]:
                    filtered[(cid, project_id)] = signal
                prev = filtered_by_sub.get((cid, project_id, project_sub_id))
                if prev is None or last >= prev["last_message_id"]:
                    filtered_by_sub[(cid, project_id, project_sub_id)] = signal
            else:
                by_project[(cid, project_id)] = signal
                by_sub[(cid, project_id, project_sub_id)] = signal
    for item in items:
        cid = item.get("channel_id")
        project_id = item.get("project_id")
        chan_ids = live_chan.get(cid) or []
        picks = [_pick(by_chan.get((cid, sid)), filtered_chan.get((cid, sid))) for sid in chan_ids]
        if chan_ids:
            item["msg_signal"] = picks[0]
        else:
            key = (cid, project_id)
            item["msg_signal"] = _pick(by_project.get(key), filtered.get(key))
        signals = [picked for picked in picks if picked is not None]
        for sub_id in sorted(subs_of.get((cid, project_id), ())):
            key3 = (cid, project_id, sub_id)
            picked = _pick(by_sub.get(key3), filtered_by_sub.get(key3))
            if picked is not None:
                signals.append(picked)
        item["msg_signals"] = signals
