"""Читающие ручки публичного API — ВСЕ SQL для GET-маршрутов живёт здесь (правило слоя, спека раздел
«Раскладка модулей»). Каждая функция первым делом накладывает project_scope принципала В САМОМ SQL —
невидимая строка не вычитывается из БД вовсе, а не отфильтровывается в Python после SELECT.

Время принципалу не нужно (кроме /status и /healthz) — оно приходит параметром `now_ms`, как везде в core.
"""

import json
from collections import defaultdict
from typing import NamedTuple

from . import agent_items as agent_items_mod
from . import attention as attention_mod
from . import autosend as autosend_mod
from . import bot_guard as bot_guard_mod
from . import buttons as buttons_mod
from . import card_route as card_route_mod
from . import chan_subs as chan_subs_mod
from . import conn_sleep as conn_sleep_mod
from . import digest as digest_mod
from . import draft_channels as drafts_mod
from . import handled as handled_mod
from . import inbound_rules as inbound_rules_mod
from . import join as join_mod
from . import leave as leave_mod
from . import media as media_mod
from . import mention_names as mention_names_mod
from . import msg_ops as msg_ops_mod
from . import mute as mute_mod
from . import new_recipient as new_recipient_mod
from . import outbox as outbox_mod
from . import pace as pace_mod
from . import peer_flood as peer_flood_mod
from . import peer_msgs as peer_msgs_mod
from . import reactions as reactions_mod
from . import reminders as reminders_mod
from . import retention
from . import rule_requests as rule_requests_mod
from . import service_bot as service_bot_mod
from . import stages as stages_mod
from . import subs as subs_mod
from . import threads as threads_mod
from . import unsorted as unsorted_mod
from . import workday as workday_mod
from .db import db_of
from .ingest import unread_breakdown
from .security import Principal

# Псевдо-проекты. «Все» (`-2`) — только UI-сессия (`principal.is_ui`); «Неразобранное» (`-1`) — UI-сессия и
# bearer со скоупом `organize` на все проекты (`principal.sees_unsorted`, спека 7a, A5/П8).
UNSORTED_PROJECT_ID = -1
ALL_PROJECTS_ID = -2

_MESSAGE_COLUMNS = (
    "id",
    "seq",
    "first_seq",
    "channel_id",
    "ext_id",
    "ext_sort",
    "thread_ext_id",
    "thread_broadcast",
    "purged",
    "reply_to_ext_id",
    "grouped_id",
    "is_me",
    "kind",
    "text",
    "entities_json",
    "attachments_json",
    "forward_json",
    "mail_to_json",
    "mentions_me",
    "refs_json",
    "reactions_json",
    "ts",
    "edited_ts",
    "deleted",
    "outbox_id",
    "sender_id",
    "sender_name",
    "sender_handle",
    "outbox_status",
    # Ф12-2 A2: черновик по графику и, пока он в очереди, — когда уйдёт.
    "outbox_paced",
    "outbox_not_before_ts",
    # Ф30-1 A2: время, к которому сессия просила отправить (`send_at`), мс UTC; у прочих — NULL.
    "outbox_send_at_ts",
    # Ф30-2 A2: догон к сроку не уйдёт, если после подтверждения в беседе ответили.
    "outbox_cancel_if_reply",
    "unread",
    "for_me",
    "attention",
    # Ф8-4: удалено владельцем в хабе (в выдачи не попадает; `/sync` отдаёт заглушку, панель треда — родителя-
    # заглушку) и число прежних версий правленого сообщения (C3, П12).
    "hidden",
    "edit_count",
    # Ф20-1 A3/B2: упоминание — только широковещательное (`@channel`/`@here`/`@everyone`), сообщение бота.
    "mentions_broadcast",
    "is_bot",
    # Ф57-1 П1: письмо-рассылка (ставит коннектор почты); читает только расчёт внимания.
    "bulk",
    # Ф23-3: просмотры поста канала Telegram (`{views, forwards, ts}`), у прочих — NULL.
    "views_json",
    # Ф37-1 C1: роль подписки сессии, чьё сообщение ушло без подтверждения владельца; у прочих — NULL.
    "outbox_auto_role",
    # Ф39 B3: роль автора черновика сессии (подписка с ролью на момент черновика); у прочих — NULL.
    "outbox_author_role",
    # Ф44-1a: инлайн-кнопки сообщения Telegram (форма события, с сырым callback) — наружу только `buttons`
    # без `d` (`buttons.public`).
    "buttons_json",
    # Ф63-1 П2/П5: совпавшие правила входящих проекта (JSON-список номеров) и внимание, назначенное правилом.
    "rule_hits",
    "rule_attention",
)


class NotFound(Exception):
    """Строка не существует ИЛИ невидима принципалу — снаружи это всегда 404, различать нельзя (спека)."""


class ServiceBotClosed(NotFound):
    """Ф54-2 (П2): личка с ботом хаба — bearer-токенам закрыта."""


def _sql(schema: str, template: str, **parts: str) -> str:
    """Подставить имя схемы и доверенные фрагменты (не пользовательский ввод, см. ingest.py) в шаблон.

    Всегда вызывать так: сам текст запроса — литерал ЗДЕСЬ, в вызове; переменные приходят только через
    `**parts`/`schema`, а не через `+`/f-string прямо в аргументе `conn.execute(...)`. Иначе ruff (S608)
    не может отличить это от конкатенации пользовательских данных — см. комментарий в ingest.py.
    """
    return template.format(schema=schema, **parts)


def _fmt(template: str, **parts: str) -> str:
    """Тот же приём, что `_sql`, но без схемы — для запросов, которые всегда идут в main."""
    return template.format(**parts)


def _deny_service_bot(conn, principal: Principal, schema: str, channel_id: int) -> None:
    """Ф54-2 (П2): личка с ботом хаба (в ней заявки «нужен человек» со скриншотами и кнопками решения)
    bearer-токенам закрыта — чтение, медиа, нажатия, черновики: `NotFound` с текстом `CLOSED_TEXT`. Владельцу
    (кука) беседа, заведённая до подключения бота, остаётся видна в архиве."""
    if not principal.is_ui and service_bot_mod.is_service_channel(conn, schema, channel_id):
        raise ServiceBotClosed(service_bot_mod.CLOSED_TEXT)


def not_found_detail(exc: Exception) -> str:
    """Текст 404 для маршрута: у служебной беседы бота хаба — её причина, у прочего — безликое `not found`
    (существует канал или он чужой, по ответу различать нельзя)."""
    return service_bot_mod.CLOSED_TEXT if isinstance(exc, ServiceBotClosed) else "not found"


def _scope_predicate(column_expr: str, principal: Principal) -> tuple[str, list]:
    """Фрагмент WHERE и параметры для фильтра по project_id. `column_expr` — то, с чем сравнивать
    (например `project_id` или `c.project_id`). `principal.project_ids is None` — все проекты (только UI).
    """
    if principal.project_ids is None:
        return "1=1", []
    ids = sorted(principal.project_ids)
    if not ids:
        return "1=0", []
    placeholders = ",".join("?" for _ in ids)
    return f"{column_expr} IN ({placeholders})", list(ids)


def _service_guard(conn, principal: Principal, id_expr: str) -> str:
    """Ф54-2 (П2): фрагмент WHERE «беседа — не служебная личка с ботом хаба». Bearer-токенам такие беседы
    не существуют ни на одном пути чтения; владельцу (кука) видны как были. `id_expr` — выражение id беседы
    (`c.id`, `id`). Числа id — из БД, не ввод: подставляются в текст запроса как есть."""
    if principal.is_ui:
        return "1=1"
    closed = service_bot_mod.channel_ids(conn)
    if not closed:
        return "1=1"
    return f"{id_expr} NOT IN ({','.join(str(int(cid)) for cid in sorted(closed))})"


def _channel_scope(conn, principal: Principal, alias: str = "c") -> tuple[str, list]:
    """Видимость БЕСЕДЫ принципалу: скоуп проекта (`_scope_predicate`) и `_service_guard`. `alias` — алиас
    таблицы `channel` в запросе (пустая строка — без алиаса). Все запросы к беседам и к тому, что в них
    лежит (сообщения, медиа, исходящие, карточки), берут предикат отсюда."""
    prefix = f"{alias}." if alias else ""
    pred, params = _scope_predicate(f"{prefix}project_id", principal)
    guard = _service_guard(conn, principal, f"{prefix}id")
    return (pred if guard == "1=1" else f"({pred}) AND {guard}"), params


def _project_exists(conn, project_id: int, principal: Principal) -> bool:
    pred, params = _scope_predicate("id", principal)
    row = conn.execute(
        _fmt("SELECT 1 FROM main.project WHERE id = ? AND ({pred})", pred=pred), [project_id, *params]
    ).fetchone()
    return row is not None


def project_visible(conn, principal: Principal, project_id: int) -> bool:
    """Настоящий проект (не псевдо-) существует и виден принципалу. Спека 7a, П4: целевой проект переноса
    канала bearer-ом проверяется и по скоупу, а не только по существованию."""
    return _project_exists(conn, project_id, principal)


def _check_pseudo_project(principal: Principal, project_id: int) -> None:
    """Спека 7a, П8: `-1` — по `sees_unsorted`, `-2` — только `is_ui`; иначе `NotFound` (404)."""
    if project_id == UNSORTED_PROJECT_ID and principal.sees_unsorted:
        return
    if project_id == ALL_PROJECTS_ID and principal.is_ui:
        return
    raise NotFound(f"проект {project_id} не существует для этого токена")


# Позиционный маркер прочитанного (находка F8 второго круга критика куска 1b): `first_seq > last_read_seq`
# ломался, когда СТАРОЕ по времени сообщение вставлялось ПОЗЖЕ нового (стартовая загрузка + живое, догон
# после сна, окно перечитывания) — новое получало БОЛЬШИЙ `first_seq`, чем более старое прочитанное, и
# ошибочно считалось прочитанным. Непрочитанное теперь — сравнение ПОЗИЦИИ (ts, ext_sort, ext_id), row-value.
#
# Раздел «Непрочитанное» спеки 2b: unread = unread_top (только top_level) + unread_threads (ответы в
# ПОДПИСАННЫХ тредах, не broadcast — тот уже top_level, не считаем дважды). LEFT JOIN на `thread` даёт каждой
# строке сообщения сводку её треда (NULL, если сообщение — не ответ или тред ещё не существует).
_MESSAGE_UNION = (
    "SELECT m.channel_id AS channel_id, m.ts AS ts, m.ext_sort AS ext_sort, m.ext_id AS ext_id, "
    "m.is_me AS is_me, m.kind AS kind, m.deleted AS deleted, m.thread_ext_id AS thread_ext_id, "
    "m.thread_broadcast AS thread_broadcast, t.subscribed AS t_subscribed, "
    "(m.mentions_me = 1 AND m.mentions_broadcast = 0) AS direct_mention, "
    "t.first_seen_ts AS t_first_seen_ts, t.last_read_ts AS t_last_read_ts, "
    "t.last_read_sort AS t_last_read_sort, t.last_read_ext AS t_last_read_ext "
    "FROM main.message m "
    "LEFT JOIN main.thread t ON t.channel_id = m.channel_id AND t.ext_id = m.thread_ext_id "
    # Ф8-4 П3: удалённое у себя в хабе не считается никуда.
    "WHERE m.hidden = 0 "
    "UNION ALL "
    "SELECT m.channel_id, m.ts, m.ext_sort, m.ext_id, m.is_me, m.kind, m.deleted, m.thread_ext_id, "
    "m.thread_broadcast, t.subscribed, (m.mentions_me = 1 AND m.mentions_broadcast = 0), "
    "t.first_seen_ts, t.last_read_ts, t.last_read_sort, t.last_read_ext "
    "FROM work.message m "
    "LEFT JOIN work.thread t ON t.channel_id = m.channel_id AND t.ext_id = m.thread_ext_id "
    "WHERE m.hidden = 0"
)
_UNREAD_DAY_MS = 24 * 60 * 60 * 1000
_UNREAD_TOP_LEVEL_PRED = threads_mod.top_level_sql("m.thread_ext_id", "m.ext_id", "m.thread_broadcast")
_UNREAD_IS_REPLY_PRED = threads_mod.is_reply_sql("m.thread_ext_id", "m.ext_id")
_UNREAD_POSITION_PRED = (
    f"(({_UNREAD_TOP_LEVEL_PRED} AND (ch.last_read_ts IS NULL OR (m.ts, m.ext_sort, m.ext_id) > "
    f"(ch.last_read_ts, ch.last_read_sort, ch.last_read_ext))) "
    f"OR ({_UNREAD_IS_REPLY_PRED} AND m.thread_broadcast = 0 "
    f"AND (m.t_subscribed = 1 OR m.direct_mention = 1) "
    f"AND m.ts >= m.t_first_seen_ts - {_UNREAD_DAY_MS} "
    f"AND (m.t_last_read_ts IS NULL OR (m.ts, m.ext_sort, m.ext_id) > "
    f"(m.t_last_read_ts, m.t_last_read_sort, m.t_last_read_ext))))"
)


def _session_fallback(raw) -> dict | None:
    from . import engine_fallback  # noqa: PLC0415 — модуль тянет записи сессий хаба

    how, value = engine_fallback.parse_session_value(raw)
    return value if how == "own" else None


def list_projects(conn, principal: Principal) -> list[dict]:
    pred_main, params_main = _channel_scope(conn, principal, "c")
    pred_work, params_work = _channel_scope(conn, principal, "c")
    # 23.10: счётчики проекта не считают беседы в архиве (как «Нужна реакция», `home_react`).
    pred_main, pred_work = f"({pred_main}) AND c.archived = 0", f"({pred_work}) AND c.archived = 0"
    pred_p, params_p = _scope_predicate("p.id", principal)

    # Ф66-3 E: серый `unread` не считает беседы в режиме внимания `muted` (как красный `unread_react`);
    # `unread` самой беседы не меняется.
    query = _fmt(
        "WITH ch AS ("
        "  SELECT c.id AS id, c.project_id AS project_id, c.last_read_ts AS last_read_ts, "
        "  c.last_read_sort AS last_read_sort, c.last_read_ext AS last_read_ext, {mode} AS mode "
        "  FROM main.channel c LEFT JOIN main.project p ON p.id = c.project_id "
        "  WHERE {pred_main} "
        "  UNION ALL SELECT c.id, c.project_id, c.last_read_ts, c.last_read_sort, c.last_read_ext, {mode} "
        "  FROM work.channel c LEFT JOIN main.project p ON p.id = c.project_id "
        "  WHERE {pred_work}"
        "), unread AS ("
        "  SELECT ch.project_id AS project_id, COUNT(*) AS cnt FROM ch "
        "  JOIN ({message_union}) m ON m.channel_id = ch.id "
        "  WHERE ch.mode <> 'muted' AND m.is_me = 0 AND m.kind <> 'event' AND m.deleted = 0 "
        "  AND {unread_pred} "
        "  GROUP BY ch.project_id"
        ") "
        "SELECT p.id, p.name, p.color, p.sort, p.muted, p.workdir, COALESCE(u.cnt, 0), p.agent_policy_json, "
        "p.unsorted_accounts, p.session_fallback_json "
        "FROM main.project p LEFT JOIN unread u ON u.project_id = p.id "
        "WHERE {pred_p} "
        "ORDER BY p.sort, p.id",
        mode=attention_mod.channel_mode_sql("c", "p"),
        pred_main=pred_main,
        pred_work=pred_work,
        message_union=_MESSAGE_UNION,
        unread_pred=_UNREAD_POSITION_PRED,
        pred_p=pred_p,
    )
    rows = conn.execute(query, [*params_main, *params_work, *params_p]).fetchall()
    # Ф8-3 A5/П5: `unread_react` проектов — одним сгруппированным запросом на схему (скоуп принципала — в
    # SQL, `NULL` — «Неразобранное»), режим — функцией SQLite `hub_attention`.
    react_by_project: dict = defaultdict(int)
    pred_c, params_c = _channel_scope(conn, principal, "c")
    pred_c = f"({pred_c}) AND c.archived = 0"
    for schema in ("main", "work"):
        counts = attention_mod.unread_react_counts(
            conn, schema, where=pred_c, params=params_c, group="c.project_id"
        )
        for key, count in counts.items():
            react_by_project[key] += count
    projects = [
        {
            "id": pid,
            "name": name,
            "color": color,
            "sort": sort,
            "muted": bool(muted),
            "workdir": workdir,
            "unread": unread,
            "unread_react": react_by_project.get(pid, 0),
            # Ф8-3 A5: ключ `attention` политики проекта (пустой словарь — политики нет).
            "attention_policy": attention_mod.policy_of(policy_json),
            # Ф13-1 B0: подписки сессий в проекте включены владельцем.
            "subs": subs_mod.subs_enabled(policy_json),
            # Ф15-1 A5: реакции сессий в проекте и их пауза.
            "session_reactions": reactions_mod.session_policy(policy_json),
            # Ф35-2 решение 9: обмен сообщениями агентов в проекте (нет ключа — включено).
            "agent_msgs": peer_msgs_mod.enabled(policy_json),
            # Ф17-1 A4: вступление сессий в группы Telegram — `confirm` (через владельца) или `auto`.
            "session_join": join_mod.session_join(policy_json),
            # Ф37-3 A: отметка «разобрано» сессиями проекта включена владельцем.
            "session_handled": handled_mod.enabled(policy_json),
            # Ф86-1 (C4): сводка дня после сжатия и слова владельца из соседних сессий (нет ключа — включено).
            **digest_mod.fields(policy_json),
            # Ф48-4 п.4: умолчание интервала пачки сообщений агентов, минуты (0 — сразу).
            "agent_batch_min": subs_mod.project_agent_batch(policy_json),
            # Ф64-1: проект «по времени», открыт ли рабочий день и время последней смены.
            **workday_mod.fields(policy_json),
            # Ф52-2 A2: аккаунты, чьё «Неразобранное» слышит проект, — только входу владельца.
            **({"unsorted_accounts": unsorted_mod.parse(unsorted_raw)} if principal.is_ui else {}),
            # Ф66-1 (A.3, П6): таблица маршрута карточек проекта — только входу владельца (сессии её не видят).
            **({card_route_mod.POLICY_KEY: card_route_mod.of_policy(policy_json)} if principal.is_ui else {}),
            # Ф83-1: запасной движок сессий хаба проекта `{engine, model, effort}` или `null` — только
            # входу владельца.
            **({"session_fallback": _session_fallback(fallback_raw)} if principal.is_ui else {}),
        }
        for pid, name, color, sort, muted, workdir, unread, policy_json, unsorted_raw, fallback_raw in rows
    ]

    projects_unread = sum(p["unread"] for p in projects)
    unsorted_unread = 0
    if principal.sees_unsorted:
        unsorted_query = _fmt(
            "WITH ch AS ("
            "  SELECT c.id AS id, c.last_read_ts AS last_read_ts, c.last_read_sort AS last_read_sort, "
            "  c.last_read_ext AS last_read_ext, {mode} AS mode "
            "  FROM main.channel c LEFT JOIN main.project p ON p.id = c.project_id "
            "  WHERE c.project_id IS NULL "
            "  UNION ALL SELECT c.id, c.last_read_ts, c.last_read_sort, c.last_read_ext, {mode} "
            "  FROM work.channel c LEFT JOIN main.project p ON p.id = c.project_id "
            "  WHERE c.project_id IS NULL"
            ") SELECT COUNT(*) FROM ch JOIN ({message_union}) m ON m.channel_id = ch.id "
            "WHERE ch.mode <> 'muted' AND m.is_me = 0 AND m.kind <> 'event' AND m.deleted = 0 "
            "AND {unread_pred}",
            mode=attention_mod.channel_mode_sql("c", "p"),
            message_union=_MESSAGE_UNION,
            unread_pred=_UNREAD_POSITION_PRED,
        )
        unsorted_unread = conn.execute(unsorted_query).fetchone()[0]
        projects.append(
            {
                "id": UNSORTED_PROJECT_ID,
                "name": "Неразобранное",
                "color": None,
                "sort": None,
                "muted": False,
                "workdir": None,
                "unread": unsorted_unread,
                "unread_react": react_by_project.get(None, 0),
                "attention_policy": None,
                "subs": False,
            }
        )
    if principal.is_ui:
        # `sees_unsorted` у UI-сессии всегда истинно — `unsorted_unread` посчитан веткой выше.
        all_unread = projects_unread + unsorted_unread
        all_react = sum(react_by_project.values())
        projects.append(
            {
                "id": ALL_PROJECTS_ID,
                "name": "Все",
                "color": None,
                "sort": None,
                "muted": False,
                "workdir": None,
                "unread": all_unread,
                "unread_react": all_react,
                "attention_policy": None,
                "subs": False,
            }
        )
    return projects


_CHANNEL_LIST_COLUMNS = (
    "c.id AS id, c.ext_id AS ext_id, c.kind AS kind, c.title AS title, c.tier AS tier, c.muted AS muted, "
    "c.archived AS archived, c.map_reason AS map_reason, c.last_msg_ts AS last_msg_ts, "
    "c.my_msg_count AS my_msg_count, c.first_seen_ts AS first_seen_ts, c.project_id AS project_id, "
    # Спека 2e, раздел «Ядро»: `source` аккаунта (telegram/slack/fake/…) — единственная правка ядра куска,
    # нужна UI, чтобы решить семантику «Ответить» (тред Slack vs. reply_to Telegram). Аккаунт всегда лежит
    # в `main.account` независимо от схемы сообщений канала (тот же приём, что `_RESOLVE_TEMPLATE`).
    "p.title AS parent_title, a.source AS source, "
    # Ф8-3 A5/П15: эффективный режим, ручной (`NULL` — авто) и режим без ручного — функцией SQLite (П5);
    # `pj` — проект канала (`p` здесь уже занят форумом-родителем).
    f"{attention_mod.channel_mode_sql('c', 'pj')} AS attention, c.attention AS attention_manual, "
    f"{attention_mod.channel_mode_sql('c', 'pj', manual=False)} AS attention_auto, "
    # Ф10-5 B3: `attrs` беседы — поля `can_reply`/`tracker` (`_reply_fields`).
    "c.meta_json AS meta_json, "
    # Ф11-7: «собеседник прочитал мои до позиции» (Telegram; прочим NULL).
    "c.peer_read_sort AS peer_read_sort, "
    # Ф12-5 П2: состояние первого сообщения беседы-заготовки (подзапрос только для `new:…`, у прочих NULL).
    "{draft_state} AS draft_state, "
    # Ф14-1 A6: источник раскладки (`auto`/`manual`) — UI показывает «По правилам» / ручную привязку.
    "c.map_source AS map_source, "
    # Ф34-1 A3/П5: стадия беседы, заметка к ней и время постановки (у беседы без стадии — NULL).
    "c.stage AS stage, c.stage_note AS stage_note, c.stage_ts AS stage_ts"
)


def _channel_columns(schema: str) -> str:
    """Колонки строки канала для файла `schema` (`draft_state` читает outbox того же файла)."""
    return _CHANNEL_LIST_COLUMNS.replace("{draft_state}", drafts_mod.draft_state_sql(schema, "c"))


_PROJECT_JOIN = "LEFT JOIN main.project pj ON pj.id = c.project_id "


def _attention_fields(attention: str, attention_manual: str | None, attention_auto: str) -> dict:
    return {"attention": attention, "attention_manual": attention_manual, "attention_auto": attention_auto}


def _stage_fields(stage: str | None, stage_note: str | None, stage_ts: int | None) -> dict:
    """Ф34-1 A3: стадия беседы в выдаче (список бесед, `GET /channels/{id}`, `/sync`); нет стадии — `null`."""
    return {"stage": stage, "stage_note": stage_note, "stage_ts": stage_ts}


TRACKER_NAMES = ("jira", "trello", "notify")
_TRACKER_KEY_MAX = 64


def _reply_fields(meta_json: str | None, draft_state: str | None = None, *, full: bool = False) -> dict:
    """Ф10-5 B3: `can_reply` — `false` у беседы с `attrs.no_reply is True` (уведомления трекеров с почты,
    ответ из хаба не уходит — `outbox.channel_no_reply`), иначе `true`; `tracker` — `{name, key, url}` только
    если `attrs.tracker` — `jira`/`trello`, `key` — непустая строка ≤ 64, `url` — строка на `https://`;
    иначе `null` (`url` UI ставит в ссылку — не-https не пропускаем).

    Ф12-5 A2 + П2: у беседы-заготовки (`new_to`) `can_reply` — истина, пока нет живой строки outbox
    (`draft_state` из SELECT выдачи — `drafts_mod.draft_state_sql`); иначе ложь с причиной.

    Ф34-3 П2: `full` — выдача UI-сессии владельца (`principal.is_ui`): только ей `new_to` вида `tg_phone` отдаётся
    с номером; bearer получает `{"kind": "tg_phone"}` (`drafts_mod.public_new_to`)."""
    attrs = None
    if meta_json:
        try:
            meta = json.loads(meta_json)
        except ValueError, TypeError:
            meta = None
        attrs = meta.get("attrs") if isinstance(meta, dict) else None
    if not isinstance(attrs, dict):
        attrs = {}
    tracker = None
    name, key, url = attrs.get("tracker"), attrs.get("key"), attrs.get("url")
    if (
        name in TRACKER_NAMES
        and isinstance(key, str)
        and 0 < len(key) <= _TRACKER_KEY_MAX
        and isinstance(url, str)
        and url.startswith("https://")
    ):
        tracker = {"name": name, "key": key, "url": url}
    # Ф12-1 A2 + Ф12-5 A2/П2: беседа-заготовка новому адресату — `new_to`; `can_reply=false` с причиной, пока
    # её первое сообщение живо (в пути или ушло без привязки). После привязки `new_to` из `attrs` уходит.
    new_to = drafts_mod.public_new_to_of(meta_json, full=full)
    if new_to is not None:
        if draft_state is None:
            return {"can_reply": True, "tracker": tracker, "new_to": new_to}
        return {
            "can_reply": False,
            "can_reply_reason": SENT_UNBOUND_REASON if draft_state == "sent" else NEW_CHANNEL_REASON,
            "tracker": tracker,
            "new_to": new_to,
        }
    return {"can_reply": not outbox_mod.channel_no_reply(meta_json), "tracker": tracker}


NEW_CHANNEL_REASON = "новая беседа — сначала уйдёт первое сообщение"
SENT_UNBOUND_REASON = "сообщение ушло — беседа привяжется, когда придёт его копия"


def _autosend_map(conn) -> dict[int, list[str]]:
    """Ф37-1 A2: поле `autosend` списка бесед — роли «без подтверждения» по беседам одним запросом (список
    короткий: его ведёт владелец руками); порядок — как у `autosend.roles`."""
    found: dict[int, list[str]] = {}
    for item in autosend_mod.list_all(conn):
        found.setdefault(item["channel_id"], []).append(item["role"])
    return found


def _unread_react_map(conn, schema: str, where: str, params: list) -> dict[int, int]:
    """Ф8-3 A5/П5: `unread_react` каналов выдачи — одним сгруппированным запросом на схему, без N+1."""
    return attention_mod.unread_react_counts(conn, schema, where=where, params=params)


def _project_channel_filter(conn, principal: Principal, project_id: int) -> tuple[str, list]:
    """Фильтр «беседы проекта» списка бесед (алиас канала — `c`): настоящий проект, видимый принципалу, либо
    псевдо-проект (`-1` «Неразобранное», `-2` «Все»); иначе `NotFound`."""
    guard = _service_guard(conn, principal, "c.id")  # Ф54-2: служебная личка с ботом хаба — не для bearer
    if project_id in (UNSORTED_PROJECT_ID, ALL_PROJECTS_ID):
        _check_pseudo_project(principal, project_id)
        base = "1=1" if project_id == ALL_PROJECTS_ID else "c.project_id IS NULL"
        return f"{base} AND {guard}", []
    if not _project_exists(conn, project_id, principal):
        raise NotFound(f"проект {project_id} не существует для этого токена")
    return f"c.project_id = ? AND {guard}", [project_id]


def list_channels(conn, principal: Principal, project_id: int, *, stage: str | None = None) -> list[dict]:
    """Спека 1d-1, п.5: топик (`kind='topic'`) несёт `parent_title` — название форума-родителя (`NULL` у
    остальных). Форумы, у которых есть дочерние топики (`parent_channel_id` другого канала указывает на них),
    из списка исключаются целиком — участник видит только топики, не сам контейнер. Родитель ищется в той же
    схеме (main/work), что и сам канал: топик и его форум всегда лежат в одном файле (общий account).

    Ф34-1 A3/П3: `stage` — фильтр по стадии: `None` — без фильтра; пустая строка (или одни пробелы) — только
    беседы без стадии; иначе — беседы этой стадии (сравнение по `stage_key`: регистр и пробелы по краям не
    важны).
    """
    channel_filter, extra_params = _project_channel_filter(conn, principal, project_id)
    if stage is not None:
        key = stages_mod.stage_key(stage)
        if key:
            channel_filter += " AND c.stage_key = ?"
            extra_params = [*extra_params, key]
        else:
            channel_filter += " AND c.stage_key IS NULL"

    query = _fmt(
        # Ф38-3 B: `account_id` — в КОНЦЕ строки (как у `_CHANNEL_ONE_TEMPLATE`): по нему hub-wake сверяет
        # беседу сводки с фильтрами подписок по аккаунтам.
        "SELECT {columns_main}, c.account_id AS account_id "
        "FROM main.channel c LEFT JOIN main.channel p ON p.id = c.parent_channel_id "
        "JOIN main.account a ON a.id = c.account_id {project_join}"
        "WHERE {filter} AND {visible_main} AND NOT EXISTS ("
        "  SELECT 1 FROM main.channel ch2 WHERE ch2.parent_channel_id = c.id"
        ") "
        "UNION ALL "
        "SELECT {columns_work}, c.account_id AS account_id "
        "FROM work.channel c LEFT JOIN work.channel p ON p.id = c.parent_channel_id "
        "JOIN main.account a ON a.id = c.account_id {project_join}"
        "WHERE {filter} AND {visible_work} AND NOT EXISTS ("
        "  SELECT 1 FROM work.channel ch2 WHERE ch2.parent_channel_id = c.id"
        ") "
        # ORDER BY в компаунд-запросе (UNION ALL) допускает только имя/позицию выходной колонки, не
        # произвольное выражение — поэтому без "last_msg_ts IS NULL" отдельным термом; NULL в SQLite и так
        # меньше любого значения, так что при DESC он уже оказывается в конце. Явный "AS" на КАЖДОЙ колонке
        # (включая last_msg_ts) в `_CHANNEL_LIST_COLUMNS` обязателен именно из-за этого JOIN: без алиаса
        # SQLite при `UNION ALL` над джойном не может сопоставить `ORDER BY last_msg_ts` с колонкой результата
        # (`OperationalError: 1st ORDER BY term does not match any column in the result set`) — воспроизведено
        # руками, без JOIN тот же голый `c.last_msg_ts` резолвился нормально.
        "ORDER BY last_msg_ts DESC",
        columns_main=_channel_columns("main"),
        columns_work=_channel_columns("work"),
        filter=channel_filter,
        project_join=_PROJECT_JOIN,
        # Ф8-4 П2: беседа, удалённая владельцем в хабе, не показывается, пока в ней нет ни одного не скрытого.
        visible_main=threads_mod.channel_visible_sql("main", "c"),
        visible_work=threads_mod.channel_visible_sql("work", "c"),
    )
    rows = conn.execute(query, [*extra_params, *extra_params]).fetchall()
    react: dict[int, int] = {}
    for schema in ("main", "work"):
        react.update(_unread_react_map(conn, schema, channel_filter, list(extra_params)))

    autosend = _autosend_map(conn)
    result = []
    for (
        cid,
        ext_id,
        kind,
        title,
        tier,
        muted,
        archived,
        map_reason,
        last_msg_ts,
        my_msg_count,
        first_seen_ts,
        pid,
        parent_title,
        source,
        attention,
        attention_manual,
        attention_auto,
        meta_json,
        peer_read_sort,
        draft_state,
        map_source,
        stage_name,
        stage_note,
        stage_ts,
        account_id,
    ) in rows:
        unread_top, unread_threads = unread_breakdown(conn, cid)
        result.append(
            {
                "id": cid,
                "ext_id": ext_id,
                "kind": kind,
                "title": title,
                "tier": tier,
                "muted": bool(muted),
                "archived": bool(archived),
                "map_reason": map_reason,
                "map_source": map_source,
                "last_msg_ts": last_msg_ts,
                "my_msg_count": my_msg_count,
                "first_seen_ts": first_seen_ts,
                "unread": unread_top + unread_threads,
                "unread_top": unread_top,
                "unread_threads": unread_threads,
                **_thread_nav(conn, cid, last_msg_ts),  # Ф66-5
                "project_id": pid,
                "parent_title": parent_title,
                "source": source,
                "account_id": account_id,  # Ф38-3 B
                **_attention_fields(attention, attention_manual, attention_auto),
                "unread_react": react.get(cid, 0),
                "peer_read_sort": peer_read_sort,
                **_reply_fields(meta_json, draft_state, full=principal.is_ui),
                **_stage_fields(stage_name, stage_note, stage_ts),
                "autosend": autosend.get(cid, []),  # Ф37-1 A2
            }
        )
    return result


def _thread_nav(conn, cid: int, last_msg_ts: int | None) -> dict:
    """Ф66-5 A1/A2: поля навигации по тредам беседы. `last_activity_ts` = max(`last_msg_ts`, последний
    `last_reply_ts` её тредов; любой живой ответ, подписан владелец или нет); `last_msg_ts` не трогаем.
    `fresh_threads` / `fresh_thread_ext` — треды с чужими живыми ответами новее маркера прочтения ТРЕДА
    (подписка не нужна; окно `first_seen − 24 ч`, как у `unread_reply_sql`);
    это только метка, не непрочитанное."""
    schema = db_of(cid)
    last_reply = conn.execute(
        _sql(schema, "SELECT MAX(last_reply_ts) FROM {schema}.thread WHERE channel_id = ?"), (cid,)
    ).fetchone()[0]
    known = [x for x in (last_msg_ts, last_reply) if x is not None]
    rows = conn.execute(
        _sql(
            schema,
            "SELECT t.ext_id FROM {schema}.thread t JOIN {schema}.message m "
            "ON m.channel_id = t.channel_id AND m.thread_ext_id = t.ext_id "
            "WHERE t.channel_id = ? AND {real_reply} AND m.is_me = 0 AND m.kind <> 'event' "
            "AND m.deleted = 0 AND m.hidden = 0 AND m.thread_broadcast = 0 "
            "AND m.ts >= COALESCE(t.first_seen_ts, 0) - ? "
            # Головная 16.10: неподписанный тред «свежий», только если ответ новее прочтения беседы, иначе
            # старые нераскрытые треды висят вечно; подписанный — по своему маркеру (он и так непрочитанное).
            "AND (t.subscribed = 1 OR m.ts > COALESCE((SELECT c.last_read_ts FROM {schema}.channel c "
            "WHERE c.id = t.channel_id), 0)) "
            "AND (t.last_read_ts IS NULL OR (m.ts, m.ext_sort, m.ext_id) > "
            "(t.last_read_ts, t.last_read_sort, t.last_read_ext)) "
            "GROUP BY t.id ORDER BY MAX(m.ts) DESC, t.id DESC",
            real_reply=threads_mod.real_reply_sql("m.thread_ext_id", "m.ext_id"),
        ),
        (cid, threads_mod._UNREAD_WINDOW_MS),
    ).fetchall()
    return {
        "last_activity_ts": max(known) if known else None,
        "fresh_threads": len(rows),
        "fresh_thread_ext": rows[0][0] if rows else None,
    }


def list_stages(conn, principal: Principal, project_id: int) -> list[dict]:
    """Ф34-1 A3 (решения 1 и 5, П3, П4): стадии бесед проекта — `[{stage, count}]`, по алфавиту ключа. Словаря
    стадий нет: это разные `stage_key` бесед проекта под ТЕМ ЖЕ предикатом, что список бесед (без
    форумов-контейнеров, без удалённых в хабе), и без архивных — `count` сходится с числом неархивных строк
    `list_channels(stage=…)`. `stage` — как записано у беседы ключа с самым свежим `stage_ts`."""
    channel_filter, params = _project_channel_filter(conn, principal, project_id)
    found: dict[str, list] = {}  # ключ -> [число, (stage_ts, id) самой свежей, её stage]
    for schema in ("main", "work"):
        rows = conn.execute(
            _sql(
                schema,
                "SELECT c.stage_key, c.stage, c.stage_ts, c.id FROM {schema}.channel c "
                "WHERE {filter} AND c.stage_key IS NOT NULL AND c.archived = 0 AND {visible} AND NOT EXISTS ("
                "  SELECT 1 FROM {schema}.channel ch2 WHERE ch2.parent_channel_id = c.id"
                ")",
                filter=channel_filter,
                visible=threads_mod.channel_visible_sql(schema, "c"),
            ),
            params,
        ).fetchall()
        for key, stage_name, stage_ts, cid in rows:
            fresh = (stage_ts or 0, cid)
            entry = found.setdefault(key, [0, fresh, stage_name])
            entry[0] += 1
            if fresh > entry[1]:
                entry[1], entry[2] = fresh, stage_name
    return [{"stage": found[key][2], "count": found[key][0]} for key in sorted(found)]


_CHANNEL_ONE_TEMPLATE = (
    "SELECT {columns}, c.account_id AS account_id, c.title_history_json AS title_history_json, "
    "NOT {visible} AS hidden "
    "FROM {schema}.channel c LEFT JOIN {schema}.channel p ON p.id = c.parent_channel_id "
    "JOIN main.account a ON a.id = c.account_id {project_join}"
    "WHERE c.id = ? AND ({pred})"
)


def _parse_title_history(title_history_json: str | None) -> list:
    if not title_history_json:
        return []
    try:
        history = json.loads(title_history_json)
    except ValueError, TypeError:
        return []
    return history if isinstance(history, list) else []


def get_channel(conn, principal: Principal, channel_id: int) -> dict:
    """Спека 7a, П2: `GET /channels/{id}` — те же поля, что строка `list_channels` (+ unread, `map_reason`,
    `source`), плюс `account_id`, `first_seen_ts`, `my_msg_count`, `title_history` (распарсенный список).
    Видимость — тот же предикат скоупа, что `channel_visible` (для bearer «все проекты» «Неразобранное» видно
    так же, как в `/sync`). Невидимый или несуществующий канал — `NotFound`."""
    schema = db_of(channel_id)
    pred, pred_params = _scope_predicate("c.project_id", principal)
    row = conn.execute(
        _sql(
            schema,
            _CHANNEL_ONE_TEMPLATE,
            columns=_channel_columns(schema),
            pred=pred,
            project_join=_PROJECT_JOIN,
            visible=threads_mod.channel_visible_sql(schema, "c"),
        ),
        [channel_id, *pred_params],
    ).fetchone()
    if row is None:
        raise NotFound(f"канал {channel_id} не найден")
    _deny_service_bot(conn, principal, schema, channel_id)
    (
        cid,
        ext_id,
        kind,
        title,
        tier,
        muted,
        archived,
        map_reason,
        last_msg_ts,
        my_msg_count,
        first_seen_ts,
        pid,
        parent_title,
        source,
        attention,
        attention_manual,
        attention_auto,
        meta_json,
        peer_read_sort,
        draft_state,
        map_source,
        stage_name,
        stage_note,
        stage_ts,
        account_id,
        title_history_json,
        hidden,
    ) = row
    unread_top, unread_threads = unread_breakdown(conn, cid)
    react = _unread_react_map(conn, schema, "c.id = ?", [cid])
    # Ф8-3 B3/П17: позиция МОЕГО последнего сообщения в беседе (включая треды; не заготовка, не удалённое) —
    # разборщик снимает кандидатов, на которые я уже ответил, по той же позиции, что `answered` карточки.
    my_last = conn.execute(
        _sql(
            schema,
            "SELECT id, ts, ext_sort FROM {schema}.message WHERE channel_id = ? AND is_me = 1 "
            "AND ext_id IS NOT NULL AND deleted = 0 AND hidden = 0 "
            "ORDER BY ts DESC, ext_sort DESC, id DESC LIMIT 1",
        ),
        (cid,),
    ).fetchone()
    return {
        "id": cid,
        "ext_id": ext_id,
        "kind": kind,
        "title": title,
        "tier": tier,
        "muted": bool(muted),
        "archived": bool(archived),
        "map_reason": map_reason,
        "map_source": map_source,
        "last_msg_ts": last_msg_ts,
        "my_msg_count": my_msg_count,
        "first_seen_ts": first_seen_ts,
        "unread": unread_top + unread_threads,
        "unread_top": unread_top,
        "unread_threads": unread_threads,
        **_thread_nav(conn, cid, last_msg_ts),  # Ф66-5
        "project_id": pid,
        "parent_title": parent_title,
        "source": source,
        "account_id": account_id,
        "title_history": _parse_title_history(title_history_json),
        **_attention_fields(attention, attention_manual, attention_auto),
        "unread_react": react.get(cid, 0),
        "last_my_message": (
            {"id": my_last[0], "ts": my_last[1], "ext_sort": my_last[2]} if my_last is not None else None
        ),
        # Ф8-4 П2: беседа удалена владельцем в хабе и нового после удаления не приходило (в списках её нет).
        "hidden": bool(hidden),
        "peer_read_sort": peer_read_sort,
        **_reply_fields(meta_json, draft_state, full=principal.is_ui),
        **_stage_fields(stage_name, stage_note, stage_ts),
        "autosend": autosend_mod.roles(conn, cid),  # Ф37-1 A2
        # Ф37-3 D3: в проекте беседы включена отметка «разобрано» сессиями (шапка `hub_read`).
        "session_handled": handled_mod.project_enabled(conn, pid),
        **_muted_by_fields(conn, schema, cid),  # Ф99-2
    }


def _muted_by_fields(conn, schema: str, cid: int) -> dict:
    """Ф99-2: беседу заглушила сессия (`hub_mute`) — роль и причина; иначе (владелец, вступление, не заглушена) —
    `None`. Отдельным запросом по первичному ключу: столбец не входит в общие шаблоны строки беседы."""
    row = conn.execute(
        _sql(schema, "SELECT muted, muted_by FROM {schema}.channel WHERE id = ?"), (cid,)
    ).fetchone()
    by = mute_mod.parse_muted_by(row[1]) if row is not None and row[0] else None
    return {
        "muted_by_role": by.get("role") if by else None,
        "muted_by_note": by.get("note") if by else None,
    }


# Спека 7b, П1/К5: поле `unread` сообщения во всех выдачах (`list_messages`, `list_thread_messages`,
# `changes_since`, родитель треда). `uch`/`ut` — LEFT JOIN канала и треда в шаблонах ниже; в `/sync` канал уже
# присоединён как `c`.
_MESSAGE_UNREAD = threads_mod.message_unread_sql("m", "uch", "ut")
_CHANGES_UNREAD = threads_mod.message_unread_sql("m", "c", "ut")
# Ф8-3 A5: поля `for_me` и `attention` сообщения во всех выдачах — те же построители, что `unread_react`
# (П12); `up`/`cp` — LEFT JOIN проекта канала в шаблонах ниже.
_MESSAGE_ATTENTION = (
    f"{attention_mod.for_me_sql('m', 'ut')}, {attention_mod.message_attention_sql('m', 'uch', 'up', 'ut')}"
)
_CHANGES_ATTENTION = (
    f"{attention_mod.for_me_sql('m', 'ut')}, {attention_mod.message_attention_sql('m', 'c', 'cp', 'ut')}"
)


class MessagePage(NamedTuple):
    messages: list[dict]
    next_before: str | None


def _message_row_to_dict(d: dict, project_id: int | None) -> dict:
    deleted = bool(d["deleted"])
    hidden = bool(d["hidden"])
    sender = None
    if d["sender_id"] is not None and not hidden:
        sender = {"id": d["sender_id"], "name": d["sender_name"], "handle": d["sender_handle"]}
    if hidden:
        # Ф8-4 П6: сюда скрытое попадает только родителем треда (заглушка «удалено у себя» в панели треда) —
        # содержимое уже очищено в БД, остатки очистки (типы вложений, ключи задач) наружу тоже не отдаём.
        d = {**d, **dict.fromkeys(_HIDDEN_BLANK_FIELDS)}
    return {
        "id": d["id"],
        "seq": d["seq"],
        "first_seq": d["first_seq"],
        "channel_id": d["channel_id"],
        "project_id": project_id,
        "ext_id": d["ext_id"],
        "thread_ext_id": d["thread_ext_id"],
        "thread_broadcast": bool(d["thread_broadcast"]),
        "purged": bool(d["purged"]),
        # Сводка треда — не null только у родителя со строкой треда (раздел E); заполняется
        # `_attach_thread_summaries` ОДНИМ сгруппированным запросом на страницу/пачку, не здесь.
        "thread": None,
        "reply_to_ext_id": d["reply_to_ext_id"],
        "grouped_id": d["grouped_id"],
        "is_me": bool(d["is_me"]),
        "kind": d["kind"],
        # Ф8-1 C1: удалённое отправителем остаётся видно — текст отдаётся вместе с
        # `deleted: true`, UI показывает его приглушённо с пометкой. Поиск (FTS) удалённое по-прежнему не
        # находит (`search`: `m.deleted = 0`), план 1.4 не меняется.
        "text": d["text"],
        "entities_json": d["entities_json"],
        "attachments_json": d["attachments_json"],
        "forward_json": d["forward_json"],
        # Ф11-8: адресаты (To+Cc) своего письма, JSON-список; у прочих — `null`.
        "mail_to_json": d["mail_to_json"],
        "mentions_me": bool(d["mentions_me"]),
        # Ф20-1 B2: упоминание меня — только `@channel`/`@here`/`@everyone` (не личное).
        "mentions_broadcast": bool(d["mentions_broadcast"]),
        # Ф20-1 A3: отправитель — бот.
        "is_bot": bool(d["is_bot"]),
        # Ф57-1 П1: письмо-рассылка (внимание уже посчитано с его учётом — поле справочное).
        "bulk": bool(d["bulk"]),
        "refs_json": d["refs_json"],
        "reactions_json": d["reactions_json"],
        "ts": d["ts"],
        "edited_ts": d["edited_ts"],
        "deleted": deleted,
        "outbox_id": d["outbox_id"],
        "outbox_status": d["outbox_status"],
        "outbox_paced": bool(d["outbox_paced"]),
        "outbox_not_before_ts": d["outbox_not_before_ts"],
        "outbox_send_at_ts": d["outbox_send_at_ts"],
        "outbox_cancel_if_reply": bool(d["outbox_cancel_if_reply"]),
        # Ф69-1 (дополнение головной к П7): строку очереди держит стоп первых сообщений новым людям —
        # `"peer_flood"`; заполняет `peer_flood.attach_held` (рядом с `attach_ops`), иначе `null`.
        "outbox_held": None,
        # Ф37-1 C1: ушло без подтверждения владельца — роль подписки сессии («агент: роль»); иначе `null`.
        "outbox_auto_role": None if hidden else d["outbox_auto_role"],
        # Ф39 B3: роль сессии-автора («я (роль)» в чтении); писал сам владелец или сессия без роли — `null`.
        "outbox_author_role": None if hidden else d["outbox_author_role"],
        "sender": sender,
        # Спека 7b, П1/К5: непрочитано ли сообщение на момент чтения — те же построители, что счётчики.
        "unread": bool(d["unread"]),
        # Ф8-3 A4/A5: для меня ли (упоминание или ответ в подписанном треде) и внимание к сообщению.
        "for_me": bool(d["for_me"]),
        "attention": d["attention"],
        # Ф8-5 A3: вложения со статусом загрузки — заполняет `media.attach_media` одним запросом на страницу.
        "media": [],
        # Ф8-4: удалено у себя в хабе (только у родителя-заглушки треда) и число прежних версий (C3, П12).
        "hidden": hidden,
        "edit_count": d["edit_count"] or 0,
        # Ф14-8 A2: правка / «удалить у всех» в очереди или неудача — заполняет `msg_ops.attach_ops`.
        "op": None,
        # Ф23-3: просмотры поста канала `{views, forwards, ts}`; не канал или ещё не снимали — `null`.
        "views": None if hidden else _parse_views(d["views_json"]),
        # Ф44-1a (2.4): кнопки под сообщением бота `{kind, rows: [[{t, k, u?, c?, pw?}]], cut?}` без сырого
        # callback; кнопок нет, скрыто или очищено — `null`. Адрес кнопки — `(id, ряд, место)` с 1.
        "buttons": None if hidden or d["purged"] else buttons_mod.public(d["buttons_json"]),
        # Ф63-1 П5: номера правил входящих проекта, совпавших при приёме (не больше 5), и внимание, которое
        # назначило правило (`react` | `info` | `null`) — пометка «правило #N» в ленте; названия — поле
        # `rules` ручек чтения (`inbound_rules.annotate`).
        "rule_hits": [] if hidden else inbound_rules_mod.parse_hits(d["rule_hits"]),
        "rule_attention": None if hidden else d["rule_attention"],
    }


def _parse_views(views_json: str | None) -> dict | None:
    """Ф23-3 п.5: `views_json` -> `{views, forwards, ts}`; битая строка или не тот вид — `None`."""
    if not views_json:
        return None
    try:
        data = json.loads(views_json)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("views"), int):
        return None
    forwards = data.get("forwards")
    ts = data.get("ts")
    return {
        "views": data["views"],
        "forwards": forwards if isinstance(forwards, int) else None,
        "ts": ts if isinstance(ts, int) else None,
    }


_HIDDEN_BLANK_FIELDS = (
    "text",
    "entities_json",
    "attachments_json",
    "forward_json",
    "mail_to_json",
    "refs_json",
    "reactions_json",
)


def _hidden_change(row, project_id: int | None) -> dict:
    """Ф8-4 B2/П3: скрытое сообщение в `/sync` — ОДНО изменение без содержимого, по которому UI убирает
    строку из ленты и тредов. `project_id` — чтобы стор нашёл, из какого проекта убирать."""
    d = dict(zip(_MESSAGE_COLUMNS, row, strict=True))
    return {
        "id": d["id"],
        "seq": d["seq"],
        "channel_id": d["channel_id"],
        "project_id": project_id,
        "thread_ext_id": d["thread_ext_id"],
        "hidden": True,
    }


def _thread_summary_map(conn, schema: str, pairs: set[tuple[int, str]]) -> dict[tuple[int, str], dict]:
    """Сводки тредов для набора родителей `(channel_id, ext_id)` — ОДНИМ запросом на схему (раздел E:
    «сводки страницы — одним сгруппированным запросом, не запросом на сообщение»). Ключ отсутствует у
    родителя без строки треда (раздел B: `reply_count=0` без ответов -> строка не создаётся).

    Поправки после opus-критика кода (T1/T9): `unread` — ЕДИНЫЙ построитель `threads_mod.unread_reply_sql`
    (окно `first_seen-24ч`, только подписанные тред), тот же самый, что и `ingest.unread_breakdown` — иначе
    «сколько непрочитанных тредов» и «список тредов с непрочитанным» расходятся (находка T1). `reply_count`
    — живым `COUNT(*)`, как раньше (не из кэша `thread.reply_count`, п.10/T2: тот существует для собственной
    нужды `touch()` — заметить смену числа ответов — и не обязан совпадать с фактурой строк, вставленных мимо
    него, например тестовыми фикстурами)."""
    if not pairs:
        return {}
    channel_ids = sorted({c for c, _ in pairs})
    ext_ids = sorted({e for _, e in pairs})
    ch_ph = ",".join("?" for _ in channel_ids)
    ext_ph = ",".join("?" for _ in ext_ids)

    thread_rows = conn.execute(
        _sql(
            schema,
            "SELECT channel_id, ext_id, last_reply_ts, subscribed, source_reply_count "
            "FROM {schema}.thread WHERE channel_id IN ({ch}) AND ext_id IN ({ext})",
            ch=ch_ph,
            ext=ext_ph,
        ),
        [*channel_ids, *ext_ids],
    ).fetchall()
    result: dict[tuple[int, str], dict] = {}
    for cid, ext_id, last_reply_ts, subscribed, source_reply_count in thread_rows:
        if (cid, ext_id) not in pairs:
            continue
        result[(cid, ext_id)] = {
            "reply_count": 0,
            "source_reply_count": source_reply_count,
            "last_reply_ts": last_reply_ts,
            "unread": 0,
            "subscribed": bool(subscribed),
        }
    if not result:
        return {}

    real_reply = threads_mod.real_reply_sql("m.thread_ext_id", "m.ext_id")
    unread_reply = threads_mod.unread_reply_sql("m", "t")
    reply_rows = conn.execute(
        _sql(
            schema,
            "SELECT m.channel_id, m.thread_ext_id, COUNT(*), SUM(CASE WHEN {unread} THEN 1 ELSE 0 END) "
            "FROM {schema}.message m JOIN {schema}.thread t "
            "ON t.channel_id = m.channel_id AND t.ext_id = m.thread_ext_id "
            "WHERE m.channel_id IN ({ch}) AND m.thread_ext_id IN ({ext}) AND {real_reply} AND m.deleted = 0 "
            "AND m.hidden = 0 GROUP BY m.channel_id, m.thread_ext_id",
            unread=unread_reply,
            real_reply=real_reply,
            ch=ch_ph,
            ext=ext_ph,
        ),
        [*channel_ids, *ext_ids],
    ).fetchall()
    for cid, thread_ext_id, reply_count, unread in reply_rows:
        key = (cid, thread_ext_id)
        if key in result:
            result[key]["reply_count"] = reply_count
            result[key]["unread"] = unread or 0

    return result


def _attach_thread_summaries(conn, schema: str, messages: list[dict]) -> None:
    """Мутирует `message["thread"]` для сообщений страницы/пачки, которые оказались родителями треда."""
    pairs = {(m["channel_id"], m["ext_id"]) for m in messages if m["ext_id"] is not None}
    summaries = _thread_summary_map(conn, schema, pairs)
    for m in messages:
        m["thread"] = summaries.get((m["channel_id"], m["ext_id"]))


_MESSAGE_LIST_TEMPLATE = (
    "SELECT m.id, m.seq, m.first_seq, m.channel_id, m.ext_id, m.ext_sort, m.thread_ext_id, "
    "m.thread_broadcast, m.purged, "
    "m.reply_to_ext_id, m.grouped_id, m.is_me, m.kind, m.text, m.entities_json, m.attachments_json, "
    "m.forward_json, m.mail_to_json, m.mentions_me, m.refs_json, m.reactions_json, m.ts, m.edited_ts, "
    "m.deleted, "
    "m.outbox_id, ct.id, ct.name, ct.handle, o.status, "
    "o.paced, CASE WHEN o.status = 'queued' THEN o.not_before_ts END, o.send_at_ts, o.cancel_if_reply, "
    "{unread}, {attention}, "
    "m.hidden, "
    "(SELECT COUNT(*) FROM {schema}.message_edit mev WHERE mev.message_id = m.id), "
    "m.mentions_broadcast, m.is_bot, m.bulk, m.views_json, o.auto_role, o.author_role, m.buttons_json, "
    "m.rule_hits, m.rule_attention "
    "FROM {schema}.message m "
    "LEFT JOIN {schema}.contact ct ON ct.id = m.sender_contact_id "
    "LEFT JOIN {schema}.outbox o ON o.id = m.outbox_id "
    "LEFT JOIN {schema}.channel uch ON uch.id = m.channel_id "
    "LEFT JOIN main.project up ON up.id = uch.project_id "
    "LEFT JOIN {schema}.thread ut ON ut.channel_id = m.channel_id AND ut.ext_id = m.thread_ext_id "
    "WHERE m.channel_id = ? AND m.hidden = 0 AND {top_level} {cursor}"
    "ORDER BY m.ts DESC, m.ext_sort DESC, m.id DESC LIMIT ?"
)


def list_messages(
    conn, principal: Principal, channel_id: int, *, before: tuple[int, int, int] | None, limit: int
) -> MessagePage:
    schema = db_of(channel_id)
    pred, pred_params = _scope_predicate("project_id", principal)
    chan = conn.execute(
        _sql(schema, "SELECT project_id FROM {schema}.channel WHERE id = ? AND ({pred})", pred=pred),
        [channel_id, *pred_params],
    ).fetchone()
    if chan is None:
        raise NotFound(f"канал {channel_id} не найден")
    _deny_service_bot(conn, principal, schema, channel_id)
    project_id = chan[0]

    params: list = [channel_id]
    cursor_clause = ""
    if before is not None:
        cursor_clause = "AND (m.ts, m.ext_sort, m.id) < (?, ?, ?) "
        params.extend(before)
    params.append(limit + 1)

    top_level = threads_mod.top_level_sql("m.thread_ext_id", "m.ext_id", "m.thread_broadcast")
    query = _sql(
        schema,
        _MESSAGE_LIST_TEMPLATE,
        cursor=cursor_clause,
        top_level=top_level,
        unread=_MESSAGE_UNREAD,
        attention=_MESSAGE_ATTENTION,
    )
    rows = conn.execute(query, params).fetchall()
    has_more = len(rows) > limit
    # Выборка — от новых к старым (лента мессенджера: последние N, потом вглубь), а страница отдаётся в
    # порядке показа, старые сверху. `next_before` — самая старая строка страницы: следующая лежит раньше.
    rows = rows[:limit][::-1]
    dicts = [dict(zip(_MESSAGE_COLUMNS, row, strict=True)) for row in rows]
    messages = [_message_row_to_dict(d, project_id) for d in dicts]
    _attach_thread_summaries(conn, schema, messages)
    media_mod.attach_media(conn, schema, messages)  # Ф8-5 A3
    msg_ops_mod.attach_ops(conn, schema, messages)  # Ф14-8 A2: поле `op` (правка/удаление у всех)
    peer_flood_mod.attach_held(conn, messages)  # Ф69-1: `outbox_held`
    reactions_mod.attach_can_react(conn, schema, messages)  # Ф15-1 A6: `can_react` писем
    mention_names_mod.attach_messages(conn, messages)  # Ф27-1 B: `mention_names` своих строк Slack

    next_before = None
    if has_more and dicts:
        oldest = dicts[0]
        next_before = f"{oldest['ts']},{oldest['ext_sort']},{oldest['id']}"
    return MessagePage(messages, next_before)


_SINGLE_MESSAGE_TEMPLATE = (
    "SELECT m.id, m.seq, m.first_seq, m.channel_id, m.ext_id, m.ext_sort, m.thread_ext_id, "
    "m.thread_broadcast, m.purged, "
    "m.reply_to_ext_id, m.grouped_id, m.is_me, m.kind, m.text, m.entities_json, m.attachments_json, "
    "m.forward_json, m.mail_to_json, m.mentions_me, m.refs_json, m.reactions_json, m.ts, m.edited_ts, "
    "m.deleted, "
    "m.outbox_id, ct.id, ct.name, ct.handle, o.status, "
    "o.paced, CASE WHEN o.status = 'queued' THEN o.not_before_ts END, o.send_at_ts, o.cancel_if_reply, "
    "{unread}, {attention}, "
    "m.hidden, "
    "(SELECT COUNT(*) FROM {schema}.message_edit mev WHERE mev.message_id = m.id), "
    "m.mentions_broadcast, m.is_bot, m.bulk, m.views_json, o.auto_role, o.author_role, m.buttons_json, "
    "m.rule_hits, m.rule_attention "
    "FROM {schema}.message m "
    "LEFT JOIN {schema}.contact ct ON ct.id = m.sender_contact_id "
    "LEFT JOIN {schema}.outbox o ON o.id = m.outbox_id "
    "LEFT JOIN {schema}.channel uch ON uch.id = m.channel_id "
    "LEFT JOIN main.project up ON up.id = uch.project_id "
    "LEFT JOIN {schema}.thread ut ON ut.channel_id = m.channel_id AND ut.ext_id = m.thread_ext_id "
    "WHERE m.channel_id = ? AND m.ext_id = ?"
)


def _find_message(conn, schema: str, channel_id: int, ext_id: str, project_id: int | None) -> dict | None:
    """Одно сообщение по `(channel_id, ext_id)` — опора `parent` в разделе E (родитель треда). Со своей
    сводкой треда, если сообщение само оказалось родителем."""
    row = conn.execute(
        _sql(schema, _SINGLE_MESSAGE_TEMPLATE, unread=_MESSAGE_UNREAD, attention=_MESSAGE_ATTENTION),
        (channel_id, ext_id),
    ).fetchone()
    if row is None:
        return None
    d = dict(zip(_MESSAGE_COLUMNS, row, strict=True))
    msg = _message_row_to_dict(d, project_id)
    _attach_thread_summaries(conn, schema, [msg])
    media_mod.attach_media(conn, schema, [msg])  # Ф8-5 A3
    msg_ops_mod.attach_ops(conn, schema, [msg])  # Ф14-8 A2: поле `op` (правка/удаление у всех)
    peer_flood_mod.attach_held(conn, [msg])  # Ф69-1: `outbox_held`
    reactions_mod.attach_can_react(conn, schema, [msg])  # Ф15-1 A6: `can_react` писем
    mention_names_mod.attach_messages(conn, [msg])  # Ф27-1 B
    return msg


# Ф10-2 A3: то же сообщение, что в ленте и у родителя треда, — по `id` (тот же SELECT, другой ключ).
_MESSAGE_BY_ID_TEMPLATE = _SINGLE_MESSAGE_TEMPLATE.replace(
    "WHERE m.channel_id = ? AND m.ext_id = ?", "WHERE m.id = ? AND m.channel_id = ?"
)
assert _MESSAGE_BY_ID_TEMPLATE != _SINGLE_MESSAGE_TEMPLATE


def get_message(conn, principal: Principal, message_id: int) -> dict:
    """Ф10-2 A3: `GET /messages/{id}` — одно сообщение в форме элемента ленты (`list_messages`): со сводкой
    треда и `media`. Видимость — `message_visible` (скоуп проекта через канал; чужое и несуществующее —
    `NotFound`). Удалённое у себя в хабе — заглушкой без содержимого, как родитель треда в ленте (Ф8-4 П6);
    удалённое источником — с текстом и `deleted: true`, как в ленте (Ф8-1 C1)."""
    channel_id = message_visible(conn, principal, message_id, include_hidden=True)
    project_id = channel_visible(conn, principal, channel_id)
    schema = db_of(message_id)
    row = conn.execute(
        _sql(schema, _MESSAGE_BY_ID_TEMPLATE, unread=_MESSAGE_UNREAD, attention=_MESSAGE_ATTENTION),
        (message_id, channel_id),
    ).fetchone()
    if row is None:
        raise NotFound(f"сообщение {message_id} не найдено")
    msg = _message_row_to_dict(dict(zip(_MESSAGE_COLUMNS, row, strict=True)), project_id)
    _attach_thread_summaries(conn, schema, [msg])
    media_mod.attach_media(conn, schema, [msg])
    msg_ops_mod.attach_ops(conn, schema, [msg])  # Ф14-8 A2: поле `op` (правка/удаление у всех)
    peer_flood_mod.attach_held(conn, [msg])  # Ф69-1: `outbox_held`
    reactions_mod.attach_can_react(conn, schema, [msg])  # Ф15-1 A6: `can_react` писем
    mention_names_mod.attach_messages(conn, [msg])  # Ф27-1 B
    return msg


_THREAD_MESSAGE_LIST_TEMPLATE = (
    "SELECT m.id, m.seq, m.first_seq, m.channel_id, m.ext_id, m.ext_sort, m.thread_ext_id, "
    "m.thread_broadcast, m.purged, "
    "m.reply_to_ext_id, m.grouped_id, m.is_me, m.kind, m.text, m.entities_json, m.attachments_json, "
    "m.forward_json, m.mail_to_json, m.mentions_me, m.refs_json, m.reactions_json, m.ts, m.edited_ts, "
    "m.deleted, "
    "m.outbox_id, ct.id, ct.name, ct.handle, o.status, "
    "o.paced, CASE WHEN o.status = 'queued' THEN o.not_before_ts END, o.send_at_ts, o.cancel_if_reply, "
    "{unread}, {attention}, "
    "m.hidden, "
    "(SELECT COUNT(*) FROM {schema}.message_edit mev WHERE mev.message_id = m.id), "
    "m.mentions_broadcast, m.is_bot, m.bulk, m.views_json, o.auto_role, o.author_role, m.buttons_json, "
    "m.rule_hits, m.rule_attention "
    "FROM {schema}.message m "
    "LEFT JOIN {schema}.contact ct ON ct.id = m.sender_contact_id "
    "LEFT JOIN {schema}.outbox o ON o.id = m.outbox_id "
    "LEFT JOIN {schema}.channel uch ON uch.id = m.channel_id "
    "LEFT JOIN main.project up ON up.id = uch.project_id "
    "LEFT JOIN {schema}.thread ut ON ut.channel_id = m.channel_id AND ut.ext_id = m.thread_ext_id "
    "WHERE m.channel_id = ? AND m.thread_ext_id = ? AND (m.ext_id IS NULL OR m.ext_id <> m.thread_ext_id) "
    "AND m.hidden = 0 {cursor}"
    "ORDER BY m.ts DESC, m.ext_sort DESC, m.id DESC LIMIT ?"
)


class ThreadMessagePage(NamedTuple):
    messages: list[dict]
    next_before: str | None
    parent: dict | None


def list_thread_messages(
    conn,
    principal: Principal,
    channel_id: int,
    thread_ext_id: str,
    *,
    before: tuple[int, int, int] | None,
    limit: int,
) -> ThreadMessagePage:
    """Раздел E: `GET /channels/{id}/threads/{thread_ext_id}/messages` — ответы треда (включая заготовки и
    broadcast), тем же курсором, что лента канала; родитель — отдельным полем, не внутри страниц. Неизвестный
    тред при видимом канале — пустая страница, не 404 (сам канал уже проверен на видимость здесь)."""
    schema = db_of(channel_id)
    pred, pred_params = _scope_predicate("project_id", principal)
    chan = conn.execute(
        _sql(schema, "SELECT project_id FROM {schema}.channel WHERE id = ? AND ({pred})", pred=pred),
        [channel_id, *pred_params],
    ).fetchone()
    if chan is None:
        raise NotFound(f"канал {channel_id} не найден")
    _deny_service_bot(conn, principal, schema, channel_id)
    project_id = chan[0]

    params: list = [channel_id, thread_ext_id]
    cursor_clause = ""
    if before is not None:
        cursor_clause = "AND (m.ts, m.ext_sort, m.id) < (?, ?, ?) "
        params.extend(before)
    params.append(limit + 1)

    query = _sql(
        schema,
        _THREAD_MESSAGE_LIST_TEMPLATE,
        cursor=cursor_clause,
        unread=_MESSAGE_UNREAD,
        attention=_MESSAGE_ATTENTION,
    )
    rows = conn.execute(query, params).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit][::-1]
    dicts = [dict(zip(_MESSAGE_COLUMNS, row, strict=True)) for row in rows]
    messages = [_message_row_to_dict(d, project_id) for d in dicts]
    media_mod.attach_media(conn, schema, messages)  # Ф8-5 A3
    msg_ops_mod.attach_ops(conn, schema, messages)  # Ф14-8 A2: поле `op` (правка/удаление у всех)
    peer_flood_mod.attach_held(conn, messages)  # Ф69-1: `outbox_held`
    reactions_mod.attach_can_react(conn, schema, messages)  # Ф15-1 A6: `can_react` писем
    mention_names_mod.attach_messages(conn, messages)  # Ф27-1 B

    next_before = None
    if has_more and dicts:
        oldest = dicts[0]
        next_before = f"{oldest['ts']},{oldest['ext_sort']},{oldest['id']}"

    parent = _find_message(conn, schema, channel_id, thread_ext_id, project_id)
    return ThreadMessagePage(messages, next_before, parent)


_CHANNEL_THREADS_TEMPLATE = (
    "WITH reply_agg AS ("
    "  SELECT m.thread_ext_id AS ext_id, COUNT(*) AS reply_count, "
    "  SUM(CASE WHEN {unread} THEN 1 ELSE 0 END) AS unread "
    "  FROM {schema}.message m JOIN {schema}.thread t "
    "  ON t.channel_id = m.channel_id AND t.ext_id = m.thread_ext_id "
    "  WHERE m.channel_id = ? AND {real_reply} AND m.deleted = 0 AND m.hidden = 0 "
    "  GROUP BY m.thread_ext_id"
    ") "
    "SELECT t.ext_id, t.last_reply_ts, t.subscribed, t.source_reply_count, "
    "COALESCE(r.reply_count, 0), COALESCE(r.unread, 0) "
    "FROM {schema}.thread t LEFT JOIN reply_agg r ON r.ext_id = t.ext_id "
    "WHERE t.channel_id = ? {unread_filter} "
    # Ф8-4 П6: тред, чей родитель удалён у себя в хабе, а видимых ответов нет, — не показывается вовсе.
    "AND (COALESCE(r.reply_count, 0) > 0 OR NOT EXISTS (SELECT 1 FROM {schema}.message hp "
    "WHERE hp.channel_id = t.channel_id AND hp.ext_id = t.ext_id AND hp.hidden = 1)) "
    "ORDER BY t.last_reply_ts DESC "
    "LIMIT ?"
)


def list_channel_threads(
    conn, principal: Principal, channel_id: int, *, unread_only: bool, limit: int
) -> list[dict]:
    """Раздел E: `GET /channels/{id}/threads?unread=1&limit=` — треды канала, свежие первыми (по
    `last_reply_ts`): `thread_ext_id`, сводка, `parent` (или null) — для подписанного треда, у которого
    родителя в ленте нет.

    Поправки после opus-критика кода (T9, тест `test_thr9`): раньше список тредов брал ВСЕ треды канала и
    строил `IN (…)` на каждый через `_thread_summary_map` — на канале с ≥ 32766 тредами это `too many SQL
    variables` (500), а до этого — полный проход всех тредов и всех ответов канала на каждый GET. Теперь
    фильтр `unread_only`, порядок и `LIMIT` — целиком в SQL (один агрегирующий подзапрос по `channel_id`,
    без списка ext_id в параметрах); `_find_message` на родителя зовётся только для УЖЕ отфильтрованной и
    ограниченной `limit` страницы. `unread` — тот же построитель `unread_reply_sql`, что и `unread_threads`
    канала (T1): список с `unread=1` — ровно те треды, что входят в счётчик канала."""
    schema = db_of(channel_id)
    pred, pred_params = _scope_predicate("project_id", principal)
    chan = conn.execute(
        _sql(schema, "SELECT project_id FROM {schema}.channel WHERE id = ? AND ({pred})", pred=pred),
        [channel_id, *pred_params],
    ).fetchone()
    if chan is None:
        raise NotFound(f"канал {channel_id} не найден")
    _deny_service_bot(conn, principal, schema, channel_id)
    project_id = chan[0]

    real_reply = threads_mod.real_reply_sql("m.thread_ext_id", "m.ext_id")
    unread_reply = threads_mod.unread_reply_sql("m", "t")
    unread_filter = "AND COALESCE(r.unread, 0) > 0" if unread_only else ""
    query = _sql(
        schema,
        _CHANNEL_THREADS_TEMPLATE,
        real_reply=real_reply,
        unread=unread_reply,
        unread_filter=unread_filter,
    )
    rows = conn.execute(query, (channel_id, channel_id, limit)).fetchall()

    result = []
    for ext_id, last_reply_ts, subscribed, source_reply_count, reply_count, unread in rows:
        summary = {
            "reply_count": reply_count,
            "source_reply_count": source_reply_count,
            "last_reply_ts": last_reply_ts,
            "unread": unread,
            "subscribed": bool(subscribed),
        }
        parent = _find_message(conn, schema, channel_id, ext_id, project_id)
        result.append({"thread_ext_id": ext_id, "thread": summary, "parent": parent})
    return result


_INBOX_COLUMNS = (
    "c.id, c.project_id, c.last_read_seq, c.last_msg_ts, c.ext_id, c.kind, c.title, c.tier, c.muted, "
    "a.source"
)


def list_inbox(conn, principal: Principal, *, project_id: int | None, tier: str | None) -> list[dict]:
    pred, pred_params = _channel_scope(conn, principal, "c")
    # 23.10: беседа в архиве убрана — во «Входящих» её нет (как в «Нужна реакция», `home_react`).
    extra_filter = " AND c.archived = 0"
    params: list = list(pred_params)

    if project_id is not None:
        if project_id in (UNSORTED_PROJECT_ID, ALL_PROJECTS_ID):
            _check_pseudo_project(principal, project_id)
            if project_id == UNSORTED_PROJECT_ID:
                extra_filter += " AND c.project_id IS NULL"
        else:
            if not _project_exists(conn, project_id, principal):
                raise NotFound(f"проект {project_id} не существует для этого токена")
            extra_filter += " AND c.project_id = ?"
            params.append(project_id)

    if tier is not None:
        extra_filter += " AND c.tier = ?"
        params.append(tier)

    # `source` — раздел «Ядро» спеки 2e: аккаунт всегда в `main.account`, даже для каналов схемы `work`.
    query = _fmt(
        "SELECT {columns} FROM main.channel c JOIN main.account a ON a.id = c.account_id "
        "WHERE ({pred}) AND {visible} {extra}",
        columns=_INBOX_COLUMNS,
        pred=pred,
        extra=extra_filter,
        visible=threads_mod.channel_visible_sql("main", "c"),
    )
    rows = conn.execute(query, params).fetchall()
    query_work = _fmt(
        "SELECT {columns} FROM work.channel c JOIN main.account a ON a.id = c.account_id "
        "WHERE ({pred}) AND {visible} {extra}",
        columns=_INBOX_COLUMNS,
        pred=pred,
        extra=extra_filter,
        visible=threads_mod.channel_visible_sql("work", "c"),
    )
    rows += conn.execute(query_work, params).fetchall()

    result = []
    for cid, pid, _last_read_seq, last_msg_ts, ext_id, kind, title, tier_, muted, source in rows:
        unread_top, unread_threads = unread_breakdown(conn, cid)
        unread = unread_top + unread_threads
        if unread <= 0:
            continue
        result.append(
            {
                "id": cid,
                "project_id": pid,
                "last_msg_ts": last_msg_ts,
                "ext_id": ext_id,
                "kind": kind,
                "title": title,
                "tier": tier_,
                "muted": bool(muted),
                "unread": unread,
                "unread_top": unread_top,
                "unread_threads": unread_threads,
                "source": source,
            }
        )
    result.sort(key=lambda c: c["last_msg_ts"] if c["last_msg_ts"] is not None else -1, reverse=True)
    return result


_SEARCH_TEMPLATE = (
    "SELECT m.id, m.channel_id, m.ts, m.ext_sort, m.text, m.deleted, c.project_id, c.title, m.is_me, "
    "m.thread_ext_id, m.thread_broadcast, m.purged, ct.name, ct.handle, m.sender_contact_id, "
    "o.author_role, o.auto_role "  # Ф39 B3: роль сессии-автора своего сообщения
    "FROM {schema}.message_fts fts "
    "JOIN {schema}.message m ON m.id = fts.rowid "
    "JOIN {schema}.channel c ON c.id = m.channel_id "
    "LEFT JOIN {schema}.outbox o ON o.id = m.outbox_id "
    # Спека 7a, П2: отправитель в выдаче поиска — LEFT JOIN, остальное без изменений.
    "LEFT JOIN {schema}.contact ct ON ct.id = m.sender_contact_id "
    "WHERE fts.message_fts MATCH ? AND m.deleted = 0 AND m.hidden = 0 AND ({pred}) {extra} "
    "ORDER BY m.ts DESC LIMIT ?"
)


# Ф14-6 A/П13: то же по расшифровкам вложений — сообщение через `media` (П3), фильтры те же; последняя
# колонка — фрагмент расшифровки вокруг совпадения.
_SEARCH_TRANSCRIPT_TEMPLATE = (
    "SELECT m.id, m.channel_id, m.ts, m.ext_sort, m.text, m.deleted, c.project_id, c.title, m.is_me, "
    "m.thread_ext_id, m.thread_broadcast, m.purged, ct.name, ct.handle, m.sender_contact_id, "
    "o.author_role, o.auto_role, "
    "snippet(transcript_fts, 0, '', '', '…', 32) "
    "FROM {schema}.transcript_fts tf "
    "JOIN {schema}.transcript t ON t.media_id = tf.rowid "
    "JOIN {schema}.media md ON md.id = t.media_id "
    "JOIN {schema}.message m ON m.id = md.message_id "
    "JOIN {schema}.channel c ON c.id = m.channel_id "
    "LEFT JOIN {schema}.outbox o ON o.id = m.outbox_id "
    "LEFT JOIN {schema}.contact ct ON ct.id = m.sender_contact_id "
    "WHERE tf.transcript_fts MATCH ? AND m.deleted = 0 AND m.hidden = 0 AND ({pred}) {extra} "
    "ORDER BY m.ts DESC LIMIT ?"
)
TRANSCRIPT_SEARCH_MARK = "голосовое: "


def search(conn, principal: Principal, *, q: str, project_id: int | None, limit: int = 50) -> list[dict]:
    """FTS5 по обоим файлам. `q` всегда идёт в индекс как ОДНА фраза — синтаксис FTS клиенту не отдаём.

    Ф14-6 П13: сообщение находится и по расшифровке его вложения; тогда в `text` — фрагмент расшифровки с
    пометкой «голосовое:» (после текста сообщения, если он есть). Сообщение одно, даже если совпали оба
    индекса или несколько вложений (совпавший текст сообщения главнее)."""
    if not q.strip():
        return []
    phrase = '"' + q.replace('"', '""') + '"'

    pred, pred_params = _channel_scope(conn, principal, "c")
    extra_filter = ""
    params: list = [phrase, *pred_params]
    if project_id is not None:
        if project_id in (UNSORTED_PROJECT_ID, ALL_PROJECTS_ID):
            _check_pseudo_project(principal, project_id)
            if project_id == UNSORTED_PROJECT_ID:
                extra_filter += " AND c.project_id IS NULL"
        else:
            if not _project_exists(conn, project_id, principal):
                raise NotFound(f"проект {project_id} не существует для этого токена")
            extra_filter += " AND c.project_id = ?"
            params.append(project_id)

    call_params = [*params, limit]
    rows = conn.execute(_sql("main", _SEARCH_TEMPLATE, pred=pred, extra=extra_filter), call_params).fetchall()
    rows += conn.execute(
        _sql("work", _SEARCH_TEMPLATE, pred=pred, extra=extra_filter), call_params
    ).fetchall()
    seen = {row[0] for row in rows}
    for schema in ("main", "work"):
        for row in conn.execute(
            _sql(schema, _SEARCH_TRANSCRIPT_TEMPLATE, pred=pred, extra=extra_filter), call_params
        ).fetchall():
            if row[0] in seen:
                continue
            seen.add(row[0])
            fragment = TRANSCRIPT_SEARCH_MARK + (row[17] or "")
            text = f"{row[4]}\n{fragment}" if row[4] else fragment
            rows.append((*row[:4], text, *row[5:17]))
    rows.sort(key=lambda r: r[2], reverse=True)
    rows = rows[:limit]

    result = []
    for row in rows:
        mid, channel_id, ts, ext_sort, text, _deleted, pid, title, is_me = row[:9]
        thread_ext_id, thread_broadcast, purged = row[9:12]
        sender_name, sender_handle, sender_contact_id = row[12:15]
        result.append(
            {
                "message_id": mid,
                "channel_id": channel_id,
                "ts": ts,
                "ext_sort": ext_sort,
                "text": text,
                "project_id": pid,
                "channel_title": title,
                "is_me": bool(is_me),
                "thread_ext_id": thread_ext_id,
                "thread_broadcast": bool(thread_broadcast),
                "purged": bool(purged),
                "sender": (
                    {"name": sender_name, "handle": sender_handle} if sender_contact_id is not None else None
                ),
                # Ф39 B3: роль сессии-автора своего сообщения (и роль авто-отправки Ф37-1) — как в ленте.
                "outbox_author_role": row[15],
                "outbox_auto_role": row[16],
            }
        )
    return result


_OUTBOX_TEMPLATE = (
    "SELECT o.id, o.channel_id, o.text, o.status, o.origin, o.error, o.attempts, o.created_ts, "
    "o.sent_ts, o.ext_id, c.project_id, o.api_token_id, o.paced, o.not_before_ts, o.send_at_ts, "
    "o.cancel_if_reply, o.auto_role, o.author_role, o.auto_session "
    "FROM {schema}.outbox o JOIN {schema}.channel c ON c.id = o.channel_id "
    "WHERE o.id = ? AND ({pred})"
)


def get_outbox(conn, principal: Principal, outbox_id: int) -> dict:
    schema = db_of(outbox_id)
    pred, pred_params = _channel_scope(conn, principal, "c")
    row = conn.execute(_sql(schema, _OUTBOX_TEMPLATE, pred=pred), [outbox_id, *pred_params]).fetchone()
    if row is None:
        raise NotFound(f"outbox {outbox_id} не найден")
    oid, channel_id, text, status, origin, error, attempts, created_ts, sent_ts, ext_id, project_id = row[:11]
    api_token_id = row[11]
    result = {
        **pace_fields(status, row[12], row[13], row[14], row[15]),
        "id": oid,
        "channel_id": channel_id,
        "project_id": project_id,
        "text": text,
        "status": status,
        "origin": origin,
        "error": error,
        "attempts": attempts,
        "created_ts": created_ts,
        "sent_ts": sent_ts,
        "ext_id": ext_id,
        # Спека 7a, П9: черновик создан этим же токеном — агент может отменить только свой.
        "own": api_token_id == principal.token_id,
        "auto_role": row[16],  # Ф37-1 C2
        "author_role": row[17],  # Ф39 B3
        # Ф41-5б (5.4): ушло веткой выключателя автопилота — сессия хаба; поле есть только у таких строк.
        **({"auto_session": row[18]} if row[18] is not None else {}),
        # Ф69-1 П7: строку очереди держит стоп первых сообщений новым людям — `"peer_flood"`, иначе `null`.
        "held": peer_flood_mod.held_of(conn, oid) if status == "queued" else None,
    }
    if status == "pending_approval":
        category = bot_guard_mod.outbox_category(conn, oid)  # Ф63-2 (П8): как в `list_outbox`
        if category is not None:
            result["bot_guard"] = category
    mention_names_mod.attach_outbox(conn, [result])  # Ф27-1 B
    return result


# Очереди, которые ждут ЧЕЛОВЕКА (верхняя полоса UI, кусок 6), и отправленное (главная, Ф8-6 A2).
# `cancelled` — уже лента, а `queued`/`sending` живут секунды и видны на самой строке.
# Ф12-2: `queued` — запланированные по графику (главная и `hub_drafts` показывают, когда уйдут).
OUTBOX_LIST_STATUSES = frozenset({"pending_approval", "queued", "unknown", "failed", "sent"})
EXCERPT_MAX = 200  # Ф8-6 A2/A3: превью — до 200 символов текста, как есть
_RUN_LINK_WINDOW_MS = 2 * 24 * 60 * 60 * 1000  # A2: `run_id` — по `draft_ids` запусков за 2 суток
WAKE_TOKEN_PREFIX = "mcp:wake-"  # noqa: S105 — префикс имени, не секрет; токены профилей hub-wake (`hub wake setup`): `mcp:wake-<project_id>`

_OUTBOX_LIST_TEMPLATE = (
    "SELECT o.id, o.channel_id, o.text, o.status, o.origin, o.error, o.attempts, o.created_ts, "
    "o.sent_ts, o.ext_id, c.project_id, c.title, o.api_token_id, o.reply_to_ext_id, o.thread_ext_id, "
    "c.kind, acc.source, pj.name, tk.name, c.ext_id, c.meta_json, o.paced, o.not_before_ts, c.account_id, "
    "o.send_at_ts, o.cancel_if_reply, o.auto_role, o.author_role, o.auto_session "
    "FROM {schema}.outbox o JOIN {schema}.channel c ON c.id = o.channel_id "
    "JOIN main.account acc ON acc.id = c.account_id "
    "LEFT JOIN main.project pj ON pj.id = c.project_id "
    "LEFT JOIN main.api_token tk ON tk.id = o.api_token_id "
    "WHERE o.status = ? AND ({pred}) {since} "
    # Ф8-4 + Ф8-6: своё сообщение, удалённое владельцем у себя в хабе, в списках outbox (главная) не показывается.
    "AND NOT EXISTS (SELECT 1 FROM {schema}.message hm WHERE hm.outbox_id = o.id AND hm.hidden = 1) "
    "ORDER BY {order} LIMIT ?"
)


def pace_fields(
    status: str,
    paced: int | None,
    not_before_ts: int | None,
    send_at_ts: int | None = None,
    cancel_if_reply: int | None = None,
) -> dict:
    """Ф12-2 A2: черновик по графику и, для строки в очереди, — когда уйдёт (`not_before_ts`). Ф30-1 A2:
    время `send_at` (мс UTC) — у черновика с точным временем, иначе `None`. Ф30-2 A2: `cancel_if_reply` —
    догон к сроку не уйдёт, если после подтверждения в беседе ответили."""
    return {
        "paced": bool(paced),
        "not_before_ts": not_before_ts if status == "queued" else None,
        "send_at_ts": send_at_ts,
        "cancel_if_reply": bool(cancel_if_reply),
    }


def excerpt(text: str | None) -> str | None:
    """Превью для главной: первые `EXCERPT_MAX` символов как есть (без очистки — UI рендерит текстом)."""
    return None if text is None else text[:EXCERPT_MAX]


_MESSAGE_BRIEF_TEMPLATE = (
    "SELECT m.id, m.channel_id, m.ext_id, m.ts, m.text, ct.name, m.rule_hits, "
    # Ф66-5 A3: ответ в треде (не broadcast, не родитель) несёт `ext_id` родителя, иначе NULL.
    "CASE WHEN m.thread_ext_id IS NOT NULL AND m.thread_ext_id <> COALESCE(m.ext_id, '') "
    "AND m.thread_broadcast = 0 THEN m.thread_ext_id END "
    "FROM {schema}.message m "
    "LEFT JOIN {schema}.contact ct ON ct.id = m.sender_contact_id WHERE m.hidden = 0 AND ({where})"
)


def _message_briefs(conn, schema: str, *, ids=(), pairs=()) -> tuple[dict, dict]:
    """Ф8-6: отправитель и превью сообщений для главной — по id и по `(channel_id, ext_id)`, одним запросом на
    вид ключа. ЕДИНСТВЕННОЕ место, где главная читает текст сообщений мимо ленты: фильтр скрытого (Ф8-4)
    добавляется здесь. Возвращает `(по id, по паре)`, значение — `{"id", "sender", "excerpt", "ts"}`."""
    by_id: dict = {}
    by_pair: dict = {}
    ids = sorted(set(ids))
    if ids:
        rows = conn.execute(
            _sql(schema, _MESSAGE_BRIEF_TEMPLATE, where="m.id IN ({})".format(",".join("?" for _ in ids))),
            ids,
        ).fetchall()
        for mid, _cid, _ext, ts, text, sender, hits, thr in rows:
            by_id[mid] = {
                "id": mid,
                "sender": sender,
                "excerpt": excerpt(text),
                "ts": ts,
                "rule_hits": inbound_rules_mod.parse_hits(hits),
                "thread_ext_id": thr,
            }
    pairs = {p for p in pairs if p[1] is not None}
    if pairs:
        channel_ids = sorted({c for c, _ in pairs})
        ext_ids = sorted({e for _, e in pairs})
        rows = conn.execute(
            _sql(
                schema,
                _MESSAGE_BRIEF_TEMPLATE,
                where="m.channel_id IN ({ch}) AND m.ext_id IN ({ext})".format(
                    ch=",".join("?" for _ in channel_ids), ext=",".join("?" for _ in ext_ids)
                ),
            ),
            [*channel_ids, *ext_ids],
        ).fetchall()
        for mid, cid, ext, ts, text, sender, hits, thr in rows:
            if (cid, ext) in pairs:
                by_pair[(cid, ext)] = {
                    "id": mid,
                    "sender": sender,
                    "excerpt": excerpt(text),
                    "ts": ts,
                    "rule_hits": inbound_rules_mod.parse_hits(hits),
                    "thread_ext_id": thr,
                }
    return by_id, by_pair


def _project_names(conn) -> dict[int, str | None]:
    return dict(conn.execute("SELECT id, name FROM main.project").fetchall())


def author_of(origin: str, token_name: str | None, project_names: dict) -> str:
    """Ф8-6 A2: автор черновика — «ты» (UI), «Claude · <проект>» для токена профиля hub-wake (по имени
    токена), иначе имя токена."""
    if origin == "ui":
        return "ты"
    if token_name and token_name.startswith(WAKE_TOKEN_PREFIX):
        tail = token_name[len(WAKE_TOKEN_PREFIX) :]
        if tail.isdigit():
            name = project_names.get(int(tail))
            return f"Claude · {name}" if name else "Claude"
    return token_name or "клиент API"


def _run_links(conn, now_ms: int) -> dict[int, int]:
    """outbox id -> id запуска Claude, в `draft_ids` которого он есть (за 2 суток; позже — главнее)."""
    links: dict[int, tuple[int, int]] = {}
    for schema in ("main", "work"):
        rows = conn.execute(
            _sql(
                schema,
                "SELECT id, started_ts, draft_ids_json FROM {schema}.agent_run "
                "WHERE started_ts >= ? AND draft_ids_json IS NOT NULL",
            ),
            (now_ms - _RUN_LINK_WINDOW_MS,),
        ).fetchall()
        for run_id, started_ts, draft_ids_json in rows:
            try:
                draft_ids = json.loads(draft_ids_json)
            except ValueError, TypeError:
                continue
            if not isinstance(draft_ids, list):
                continue
            for oid in draft_ids:
                if isinstance(oid, int) and (oid not in links or links[oid] < (started_ts, run_id)):
                    links[oid] = (started_ts, run_id)
    return {oid: run_id for oid, (_ts, run_id) in links.items()}


def list_outbox(
    conn, principal: Principal, *, status: str, limit: int, since: int | None = None, now_ms: int = 0
) -> list[dict]:
    """Записи outbox в статусе `status` по ОБОИМ файлам, новые сверху. Фильтр проектов — в SQL, как везде.
    Ф8-6 A2: `since` — `sent_ts >= since` для `sent`, `created_ts >= since` для остальных; к записи — адрес
    (проект, беседа, источник, вид), автор, на что ответ, родитель треда и запуск Claude."""
    if status not in OUTBOX_LIST_STATUSES:
        raise ValueError(f"статус {status!r} списком не отдаётся")
    pred, pred_params = _channel_scope(conn, principal, "c")
    ts_column = "o.sent_ts" if status == "sent" else "o.created_ts"
    since_clause = f"AND {ts_column} >= ?" if since is not None else ""
    order = f"{ts_column} DESC, o.id DESC"
    rows: list = []
    for schema in ("main", "work"):
        params = [status, *pred_params, *([since] if since is not None else []), limit]
        part = conn.execute(
            _sql(schema, _OUTBOX_LIST_TEMPLATE, pred=pred, since=since_clause, order=order), params
        ).fetchall()
        rows.extend((schema, r) for r in part)
    ts_index = 8 if status == "sent" else 7
    rows.sort(key=lambda sr: (sr[1][ts_index] or 0, sr[1][0]), reverse=True)
    rows = rows[:limit]

    briefs: dict[str, dict] = {}
    for schema in ("main", "work"):
        pairs = set()
        for s, r in rows:
            if s == schema:
                pairs.add((r[1], r[13]))
                pairs.add((r[1], r[14]))
        briefs[schema] = _message_briefs(conn, schema, pairs=pairs)[1] if pairs else {}
    project_names = _project_names(conn)
    run_links = _run_links(conn, now_ms)
    files: dict[int, list[dict]] = {}
    for schema in ("main", "work"):
        files.update(outbox_files(conn, schema, [r[0] for s, r in rows if s == schema]))

    held = peer_flood_mod.held_map(conn, [(r[0], r[3], r[23], r[1]) for _s, r in rows])  # Ф69-1 П7
    result = []
    for schema, r in rows:
        oid, channel_id, reply_to_ext, thread_ext = r[0], r[1], r[13], r[14]
        reply_to = None
        if reply_to_ext is not None:
            brief = briefs[schema].get((channel_id, reply_to_ext)) or {}
            reply_to = {
                "ext_id": reply_to_ext,
                "sender": brief.get("sender"),
                "excerpt": brief.get("excerpt"),
            }
        thread = None
        if thread_ext is not None:
            brief = briefs[schema].get((channel_id, thread_ext)) or {}
            thread = {"ext_id": thread_ext, "excerpt": brief.get("excerpt")}
        result.append(
            {
                **pace_fields(r[3], r[21], r[22], r[24], r[25]),
                "account_id": r[23],
                "id": oid,
                "channel_id": channel_id,
                "text": r[2],
                "status": r[3],
                "origin": r[4],
                "error": r[5],
                "attempts": r[6],
                "created_ts": r[7],
                "sent_ts": r[8],
                "ext_id": r[9],
                "project_id": r[10],
                "channel_title": r[11],
                "own": r[12] == principal.token_id,
                "project_name": r[17] if r[10] is not None else "Неразобранное",
                "source": r[16],
                "kind": r[15],
                "token_name": r[18],
                "author": author_of(r[4], r[18], project_names),
                "reply_to": reply_to,
                "thread": thread,
                "run_id": run_links.get(oid),
                # Ф12-1 C2: черновик новому адресату — «кому» на карточке (только пока беседа — заготовка).
                # Ф34-3 П2: номер телефона (`tg_phone`) — только UI-сессии.
                "new_to": (
                    drafts_mod.public_new_to_of(r[20], full=principal.is_ui)
                    if drafts_mod.is_temp_ext_id(r[19])
                    else None
                ),
                # Ф14-3 A9 + П9: файлы черновика — владелец видит их в карточке до «Отправить».
                "files": files.get(oid, []),
                # Ф37-1 C2: ушло без подтверждения владельца — роль подписки сессии; иначе `null`.
                "auto_role": r[26],
                # Ф39 B3: роль сессии-автора черновика; иначе `null`.
                "author_role": r[27],
                # Ф41-5б (5.4): ушло веткой выключателя автопилота — сессия хаба (иначе поля нет).
                **({"auto_session": r[28]} if r[28] is not None else {}),
                # Ф63-2 (П8): черновик в беседе с ботом, который страховка не пустила бы «сразу», — категория
                # (`payment` | `secret` | `delete` | `pdn` | `file`); вычисляется при чтении, поля иначе нет.
                **_bot_guard_field(conn, oid, r[3], r[16], r[15], r[19], r[20]),
                # Ф69-1 П7: строку очереди держит стоп первых сообщений новым людям — `"peer_flood"`.
                "held": held.get(oid),
            }
        )
    mention_names_mod.attach_outbox(conn, result)  # Ф27-1 B: `mention_names` строк Slack
    return result


def _bot_guard_field(conn, outbox_id: int, status: str, source, kind, ext_id, meta_json) -> dict:
    """Ф63-2 (П8): `{"bot_guard": категория}` у ждущего черновика в беседе с ботом, если страховка
    сработала бы; иначе `{}`."""
    if status != "pending_approval" or not bot_guard_mod.chat_is_bot(source, kind, ext_id, meta_json):
        return {}
    category = bot_guard_mod.outbox_category(conn, outbox_id)
    return {"bot_guard": category} if category is not None else {}


def outbox_files(conn, schema: str, outbox_ids: list[int]) -> dict[int, list[dict]]:
    """Ф14-3 A9 + П9: файлы строк outbox — из строк `media` сообщения с `message.outbox_id = o.id` (после эха
    заготовка и её `media` переезжают на настоящую строку) в порядке `idx`: `{media_id, name, size, kind,
    mime}`, без `upload_id`. Полный список гарантирован для `pending_approval`/`queued`; у скрытого сообщения
    и после «удалить у себя» строк `media` нет — `[]`."""
    if not outbox_ids:
        return {}
    ids = sorted(set(outbox_ids))
    rows = conn.execute(
        _sql(
            schema,
            "SELECT m.outbox_id, md.id, md.name, md.size, md.kind, md.mime FROM {schema}.media md "
            "JOIN {schema}.message m ON m.id = md.message_id "
            "WHERE m.outbox_id IN ({ph}) AND m.hidden = 0 ORDER BY m.outbox_id, m.id, md.idx",
            ph=",".join("?" for _ in ids),
        ),
        ids,
    ).fetchall()
    result: dict[int, list[dict]] = {}
    for outbox_id, media_id, name, size, kind, mime in rows:
        result.setdefault(outbox_id, []).append(
            {"media_id": media_id, "name": name, "size": size, "kind": kind, "mime": mime}
        )
    return result


# --- Правки и отказы черновиков (Ф19-1, docs/architecture.md и поправки П1–П3, П7, П9) ---

FEEDBACK_INSTEAD_BEFORE_MS = 10 * 60 * 1000  # E: «написал сам» — от 10 мин до отказа…
FEEDBACK_INSTEAD_AFTER_MS = 30 * 60 * 1000  # …до 30 мин после

_FEEDBACK_COLUMNS = (
    "o.id, o.channel_id, o.text, o.status, o.created_ts, o.thread_ext_id, o.file_json, c.project_id, "
    "c.title, acc.source"
)
_FEEDBACK_JOINS = (
    "JOIN {schema}.channel c ON c.id = o.channel_id JOIN main.account acc ON acc.id = c.account_id"
)
# П3: своё сообщение, удалённое владельцем у себя в хабе, не отдаётся — как в `list_outbox`.
_FEEDBACK_NOT_HIDDEN = (
    "NOT EXISTS (SELECT 1 FROM {schema}.message hm WHERE hm.outbox_id = o.id AND hm.hidden = 1)"
)
# E + П7: правленые — по времени правки (`draft_ts`, частичный индекс 0032).
_FEEDBACK_EDITED_TEMPLATE = (
    "SELECT {cols}, o.draft_text, o.draft_ts FROM {schema}.outbox o {joins} "
    "WHERE o.draft_text IS NOT NULL AND ({pred}) {extra} AND {not_hidden} "
    "ORDER BY o.draft_ts DESC, o.id DESC LIMIT ?"
)
# E + П2: отклонённые — отмена владельца в UI (`outbox_cancel`, `actor = 'ui'`; индекс `audit_action_ts`)
# черновика сессии, текст не стёрт. Подтверждённые раньше отсекаются в Python (`_approved_outbox_ids`).
_FEEDBACK_REJECTED_TEMPLATE = (
    "SELECT {cols}, a.ts FROM main.audit a JOIN {schema}.outbox o ON o.id = a.outbox_id {joins} "
    "WHERE a.action = 'outbox_cancel' AND a.actor = 'ui' AND o.status = 'cancelled' AND o.origin = 'api' "
    # Ф37-1 П7: авто-строка, которую владелец отменил в `queued`, — не «отклонённый черновик».
    "AND o.auto_role IS NULL "
    "AND o.text <> '' AND ({pred}) {extra} AND {not_hidden} "
    "ORDER BY a.ts DESC, o.id DESC"
)
# П1 + П9: «написал сам» — только сообщения владельца: не заготовки черновиков сессий и не их подтверждённые
# черновики (`outbox.origin = 'api'`), а набранное в хабе (`origin = 'ui'`) или прямо в источнике; тот же
# тред. Ревью: элементы 1..N−1 альбома черновика сессии (`_apply_album`, `outbox_id` NULL) — тоже не владельца:
# в беседе есть элемент того же `grouped_id` со строкой очереди `origin = 'api'`.
_FEEDBACK_INSTEAD_TEMPLATE = (
    "SELECT m.id, m.text, m.ts FROM {schema}.message m "
    "WHERE m.channel_id = ? AND m.is_me = 1 AND m.deleted = 0 AND m.hidden = 0 AND m.kind <> 'event' "
    "AND (m.outbox_id IS NULL OR EXISTS (SELECT 1 FROM {schema}.outbox x WHERE x.id = m.outbox_id "
    "AND x.origin = 'ui' AND x.status <> 'cancelled')) "
    "AND NOT (m.grouped_id IS NOT NULL AND EXISTS (SELECT 1 FROM {schema}.message g "
    "JOIN {schema}.outbox gx ON gx.id = g.outbox_id WHERE g.channel_id = m.channel_id "
    "AND g.grouped_id = m.grouped_id AND gx.origin = 'api')) "
    "AND {thread} AND m.ts > ? AND m.ts <= ? ORDER BY m.ts, m.id LIMIT 1"
)


def _feedback_sql(schema: str, template: str, **parts: str) -> str:
    """Шаблон выдачи правок/отказов: общие колонки, связи и фильтр скрытого — доверенные фрагменты."""
    return template.format(
        schema=schema,
        cols=_FEEDBACK_COLUMNS,
        joins=_FEEDBACK_JOINS.format(schema=schema),
        not_hidden=_FEEDBACK_NOT_HIDDEN.format(schema=schema),
        **parts,
    )


def _approved_outbox_ids(conn) -> set[int]:
    """П2: строки, которые владелец подтверждал (`outbox_approve` в журнале) — их отмена потом не «отказ»."""
    rows = conn.execute(
        "SELECT DISTINCT outbox_id FROM main.audit WHERE action = 'outbox_approve' AND outbox_id IS NOT NULL"
    ).fetchall()
    return {r[0] for r in rows}


def _feedback_item(row, kind: str, *, draft_text: str, decided_ts: int) -> dict:
    oid, channel_id, text, status, created_ts, thread_ext, file_json, project_id, title, source = row[:10]
    return {
        "id": oid,
        "kind": kind,
        "channel_id": channel_id,
        "channel_title": title,
        "project_id": project_id,
        "source": source,
        "created_ts": created_ts,
        "thread_ext_id": thread_ext,
        # E: файлы — только число (имён не нужно); из `file_json`: у отменённой до попытки строки `media` нет.
        "files": len(outbox_mod._parse_file_json(file_json) or []),
        "status": status,
        "draft_text": draft_text,
        "text": text,
        "decided_ts": decided_ts,
    }


def _feedback_instead(conn, schema: str, item: dict) -> dict | None:
    """E + П1 + П9: первое своё сообщение владельца в той же беседе (и том же треде) в окне вокруг отказа."""
    thread_ext = item["thread_ext_id"]
    thread = "m.thread_ext_id = ?" if thread_ext is not None else "m.thread_ext_id IS NULL"
    params = [
        item["channel_id"],
        *([thread_ext] if thread_ext is not None else []),
        item["decided_ts"] - FEEDBACK_INSTEAD_BEFORE_MS,
        item["decided_ts"] + FEEDBACK_INSTEAD_AFTER_MS,
    ]
    row = conn.execute(_sql(schema, _FEEDBACK_INSTEAD_TEMPLATE, thread=thread), params).fetchone()
    if row is None:
        return None
    return {"message_id": row[0], "text": row[1], "ts": row[2]}


def draft_feedback(
    conn, principal: Principal, *, limit: int, since: int | None = None, channel_id: int | None = None
) -> list[dict]:
    """Ф19-1 E: как владелец правил (`edited`) и отклонял (`rejected`) черновики — по обоим файлам, в скоупе
    принципала, новые сверху по времени решения (`decided_ts`). `since` — по нему же (мс); `channel_id` —
    одна беседа. У отклонённого — `instead`: что владелец написал сам (или `null`)."""
    pred, pred_params = _channel_scope(conn, principal, "c")
    approved = _approved_outbox_ids(conn)
    found: list[tuple[str, dict]] = []
    for schema in ("main", "work"):
        extra, extra_params = "", []
        if since is not None:
            extra += " AND o.draft_ts >= ?"
            extra_params.append(since)
        if channel_id is not None:
            extra += " AND o.channel_id = ?"
            extra_params.append(channel_id)
        for row in conn.execute(
            _feedback_sql(schema, _FEEDBACK_EDITED_TEMPLATE, pred=pred, extra=extra),
            [*pred_params, *extra_params, limit],
        ):
            draft_ts = row[11] if row[11] is not None else row[4]
            found.append((schema, _feedback_item(row, "edited", draft_text=row[10], decided_ts=draft_ts)))

        extra, extra_params = "", []
        if since is not None:
            extra += " AND a.ts >= ?"
            extra_params.append(since)
        if channel_id is not None:
            extra += " AND o.channel_id = ?"
            extra_params.append(channel_id)
        taken: set[int] = set()
        for row in conn.execute(
            _feedback_sql(schema, _FEEDBACK_REJECTED_TEMPLATE, pred=pred, extra=extra),
            [*pred_params, *extra_params],
        ):
            if len(taken) >= limit:
                break
            if row[0] in approved or row[0] in taken:
                continue
            taken.add(row[0])
            # E: «было» — как предлагала сессия (текст строки не менялся: правки без подтверждения нет).
            found.append((schema, _feedback_item(row, "rejected", draft_text=row[2], decided_ts=row[10])))

    found.sort(key=lambda si: (si[1]["decided_ts"], si[1]["id"]), reverse=True)
    result = []
    for schema, item in found[:limit]:
        if item["kind"] == "rejected":
            item["instead"] = _feedback_instead(conn, schema, item)
        result.append(item)
    return result


# --- Журнал запусков Claude и «Нужна реакция» (Ф8-6, docs/architecture.md, A3) --------------

_AGENT_RUNS_TEMPLATE = (
    "SELECT r.id, r.project_id, r.kind, r.status, r.started_ts, r.finished_ts, r.card_ids_json, "
    "r.channel_ids_json, r.draft_ids_json, r.result, r.failure, r.session_id, r.num_turns, r.duration_ms, "
    "pj.name, pj.color "
    "FROM {schema}.agent_run r LEFT JOIN main.project pj ON pj.id = r.project_id "
    "WHERE ({pred}) {extra} ORDER BY r.started_ts DESC, r.id DESC LIMIT ?"
)


def _json_ids(raw: str | None) -> list[int]:
    try:
        value = json.loads(raw) if raw else []
    except ValueError, TypeError:
        return []
    return [x for x in value if isinstance(x, int)] if isinstance(value, list) else []


def _channel_titles(conn, principal: Principal, channel_ids: set[int]) -> dict[int, str | None]:
    """Названия бесед, ВИДИМЫХ принципалу (скоуп — в SQL), одним запросом на файл."""
    titles: dict[int, str | None] = {}
    pred, pred_params = _channel_scope(conn, principal, "")
    for schema in ("main", "work"):
        ids = sorted(cid for cid in channel_ids if db_of(cid) == schema)
        if not ids:
            continue
        rows = conn.execute(
            _sql(
                schema,
                "SELECT id, title FROM {schema}.channel WHERE id IN ({ph}) AND ({pred})",
                ph=",".join("?" for _ in ids),
                pred=pred,
            ),
            [*ids, *pred_params],
        ).fetchall()
        titles.update(dict(rows))
    return titles


def list_agent_runs(
    conn, principal: Principal, *, since: int | None, project_id: int | None, limit: int
) -> list[dict]:
    """`GET /agent/runs` (A1): запуски по ОБОИМ файлам, новые сверху; проектный скоуп — в SQL. Беседы — с
    названиями (`channels`: `{id, title}` только видимых принципалу; `channel_titles` — те же названия)."""
    pred, pred_params = _scope_predicate("r.project_id", principal)
    extra = ""
    params: list = list(pred_params)
    if since is not None:
        extra += " AND r.started_ts >= ?"
        params.append(since)
    if project_id is not None:
        if not _project_exists(conn, project_id, principal):
            raise NotFound(f"проект {project_id} не существует для этого токена")
        extra += " AND r.project_id = ?"
        params.append(project_id)
    rows: list = []
    for schema in ("main", "work"):
        query = _sql(schema, _AGENT_RUNS_TEMPLATE, pred=pred, extra=extra)
        rows.extend(conn.execute(query, [*params, limit]).fetchall())
    rows.sort(key=lambda r: (r[4], r[0]), reverse=True)
    rows = rows[:limit]
    channel_ids = {cid for r in rows for cid in _json_ids(r[7])}
    titles = _channel_titles(conn, principal, channel_ids)
    result = []
    for r in rows:
        channels = [{"id": cid, "title": titles[cid]} for cid in _json_ids(r[7]) if cid in titles]
        result.append(
            {
                "id": r[0],
                "project_id": r[1],
                "project_name": r[14],
                "project_color": r[15],
                "kind": r[2],
                "status": r[3],
                "started_ts": r[4],
                "finished_ts": r[5],
                "card_ids": _json_ids(r[6]),
                "channels": channels,
                "channel_titles": [c["title"] for c in channels],
                "draft_ids": _json_ids(r[8]),
                "result": r[9],
                "failure": r[10],
                "session_id": r[11],
                "num_turns": r[12],
                "duration_ms": r[13],
            }
        )
    return result


HOME_CARD_LABELS = ("urgent", "reply")
_HOME_CARDS_LIMIT = 200


def _rule_titles(conn, principal: Principal, hits: list[int]) -> list[dict]:
    """Ф63-1: `rules` строки «Нужна реакция» (`[{id, title}]`, как у сообщения ленты)."""
    holder = [{"rule_hits": hits}]
    inbound_rules_mod.annotate(conn, holder, principal.project_ids)
    return holder[0]["rules"]


def home_react(conn, principal: Principal) -> list[dict]:
    """`GET /home/react` (A3): беседы с `unread_react > 0` по всем проектам в скоупе + беседы Неразобранного с
    открытой непрочитанной карточкой `urgent`/`reply`. Счётчик и «последнее требующее реакции» — тем же
    предикатом, что `unread_react` (`attention.unread_react_pred`); карточки — `list_agent_items`; беседа —
    `get_channel` (поля как в списке каналов). Сортировка — по `last.ts` убыв.

    Ф34-2 решение 6: сработавшие напоминания — своими строками с ключом `reminder` (у строк бесед он `None`):
    `project` — проект НАПОМИНАНИЯ (где поставлено; без проекта — «Неразобранное»), `channel` — беседа или
    `None`, если она не видна принципалу (П10), `last` — `None`; место в сортировке — по `fired_ts`."""
    pred, pred_params = _channel_scope(conn, principal, "c")
    # 23.10: беседа в архиве убрана — её непрочитанное «Нужна реакция» (и «Ждёт» телефона) не держит.
    pred = f"({pred}) AND c.archived = 0"
    if not principal.sees_unsorted:
        pred = f"({pred}) AND c.project_id IS NOT NULL"
    react_ids: set[int] = set()
    for schema in ("main", "work"):
        counts = attention_mod.unread_react_counts(conn, schema, where=pred, params=list(pred_params))
        react_ids.update(cid for cid, count in counts.items() if count > 0)

    # Открытые карточки urgent/reply — самая свежая на беседу (выдача уже новые сверху).
    cards: dict[int, dict] = {}
    for label in HOME_CARD_LABELS:
        open_cards = list_agent_items(
            conn,
            principal,
            status="new",
            project_id=None,
            label=label,
            channel_id=None,
            limit=_HOME_CARDS_LIMIT,
        )
        for card in open_cards:
            prev = cards.get(card["channel_id"])
            if prev is None or (card["updated_ts"], card["id"]) > (prev["updated_ts"], prev["id"]):
                cards[card["channel_id"]] = card
    card_only = {
        cid
        for cid, card in cards.items()
        if card["project_id"] is None
        and principal.sees_unsorted
        and not card["read"]
        and cid not in react_ids
    }

    last: dict[int, dict] = {}
    for schema in ("main", "work"):
        ids = sorted(cid for cid in react_ids if db_of(cid) == schema)
        heads = attention_mod.last_unread_react(conn, schema, ids)
        card_msgs = {cid: cards[cid]["last_message_id"] for cid in card_only if db_of(cid) == schema}
        by_id, _ = _message_briefs(conn, schema, ids=[*(m for m, _ts in heads.values()), *card_msgs.values()])
        for cid, (mid, _ts) in heads.items():
            if mid in by_id:
                last[cid] = by_id[mid]
        for cid, mid in card_msgs.items():
            if mid in by_id:
                last[cid] = by_id[mid]

    drafts: dict[int, int] = {}
    for schema in ("main", "work"):
        ids = sorted(cid for cid in last if db_of(cid) == schema)
        if not ids:
            continue
        rows = conn.execute(
            _sql(
                schema,
                "SELECT channel_id, COUNT(*) FROM {schema}.outbox WHERE status = 'pending_approval' "
                "AND channel_id IN ({ph}) GROUP BY channel_id",
                ph=",".join("?" for _ in ids),
            ),
            ids,
        ).fetchall()
        drafts.update(dict(rows))

    projects = {
        pid: {"id": pid, "name": name, "color": color}
        for pid, name, color in conn.execute("SELECT id, name, color FROM main.project").fetchall()
    }
    unsorted = {"id": UNSORTED_PROJECT_ID, "name": "Неразобранное", "color": None}
    result = []
    for cid, brief in last.items():
        try:
            channel = get_channel(conn, principal, cid)
        except NotFound:
            continue
        card = cards.get(cid)
        result.append(
            {
                "channel": channel,
                "project": projects.get(channel["project_id"], unsorted),
                "last": {
                    "sender": brief["sender"],
                    "excerpt": brief["excerpt"],
                    "ts": brief["ts"],
                    "message_id": brief["id"],
                    "thread_ext_id": brief.get("thread_ext_id"),  # Ф66-5 A3
                },
                "rule_hits": brief["rule_hits"],
                "rules": _rule_titles(conn, principal, brief["rule_hits"]),
                "card": (
                    {"id": card["id"], "label": card["label"], "summary": card["summary"]}
                    if card is not None
                    else None
                ),
                "draft_count": drafts.get(cid, 0),
                "reminder": None,
            }
        )
    for reminder in reminders_mod.home_items(conn, principal):
        channel = None
        if reminder["channel"] is not None:
            try:
                channel = get_channel(conn, principal, reminder["channel_id"])
            except NotFound:
                channel = None
        result.append(
            {
                "channel": channel,
                "project": projects.get(reminder["project_id"], unsorted),
                "last": None,
                "card": None,
                "draft_count": 0,
                "reminder": reminder,
            }
        )
    result.sort(key=_home_sort_key, reverse=True)
    return result


def _home_sort_key(item: dict) -> tuple[int, int]:
    """Строка беседы — время и id последнего сообщения; строка напоминания — когда сработало."""
    if item["reminder"] is not None:
        return item["reminder"]["fired_ts"] or 0, item["reminder"]["id"]
    return item["last"]["ts"], item["last"]["message_id"]


def _new_message_fields(source: str | None, config_json: str | None) -> dict:
    """Ф12-5 A6: `new_message` — Telegram и почта, кроме ящика «только на приём»; `address` — адрес ящика
    почты из конфига (прочим `null`). Ф34-3: у Telegram — `tg_phone_daily` (сколько первых сообщений по
    номеру телефона в сутки; подсказка формы). Ф62-1: WhatsApp — пишет только по номеру; в том же поле
    формы — `wa_phone_daily` аккаунта."""
    is_mail = source == "mail"
    by_phone = source in ("telegram", "whatsapp")
    return {
        **({"tg_phone_daily": new_recipient_mod.phone_daily(source, config_json)} if by_phone else {}),
        "new_message": by_phone or (is_mail and new_recipient_mod.mail_send_enabled(config_json)),
        "address": new_recipient_mod.mail_address(config_json) if is_mail else None,
    }


def get_status(conn, principal: Principal, now_ms: int) -> dict:
    """Аккаунты — только те, у кого есть хоть один видимый канал (иначе полная приватность аккаунта
    протекала бы токену, который ни одного его канала не видит). `max_seq` — глобальный, как в /healthz:
    это счётчик активности, не текст и не идентификатор чего-либо конкретного.
    """
    if principal.project_ids is None:
        account_rows = conn.execute(
            "SELECT id, source, title, status, last_ok_ts, last_error, flood_until, config_json, "
            "peer_flood_ts "
            "FROM main.account ORDER BY id"
        ).fetchall()
    else:
        pred, pred_params = _channel_scope(conn, principal, "")
        visible_ids: set[int] = set()
        for schema in ("main", "work"):
            rows = conn.execute(
                _sql(schema, "SELECT DISTINCT account_id FROM {schema}.channel WHERE ({pred})", pred=pred),
                pred_params,
            ).fetchall()
            visible_ids.update(r[0] for r in rows)
        if not visible_ids:
            account_rows = []
        else:
            placeholders = ",".join("?" for _ in visible_ids)
            account_rows = conn.execute(
                _fmt(
                    "SELECT id, source, title, status, last_ok_ts, last_error, flood_until, config_json, "
                    "peer_flood_ts FROM main.account WHERE id IN ({placeholders}) ORDER BY id",
                    placeholders=placeholders,
                ),
                list(visible_ids),
            ).fetchall()

    accounts = [
        {
            "id": aid,
            "source": source,
            "title": title,
            "status": status,
            "last_ok_ts": last_ok_ts,
            # Текст ошибки коннектора произволен (имена, адреса) — токену с ограничением проектов не отдаём.
            "last_error": last_error if principal.is_ui else None,
            "flood_until": flood_until,
            "peer_flood_ts": peer_flood_ts,  # Ф69-1 П7: стоп первых сообщений новым людям
            # Ф12-5 A6: «Новое сообщение» из UI — с какого аккаунта можно и подпись ящика (только UI-сессии).
            **(_new_message_fields(source, config_json) if principal.is_ui else {}),
        }
        for aid, source, title, status, last_ok_ts, last_error, flood_until, config_json, peer_flood_ts in (
            account_rows
        )
    ]

    for account in accounts:
        # Ф12-2 A5: сколько по графику на сегодня (локальный день окна аккаунта) и суточный лимит.
        account["paced_today"], account["pace_daily"] = pace_mod.today(conn, account["id"], now_ms)
        # Ф30-1 A5: при активном прогреве — его настройки (лимит сегодня уже в `pace_daily`).
        warmup = pace_mod.warmup_status(conn, account["id"], now_ms)
        if warmup is not None:
            account["pace_warmup"] = warmup
        # Ф70-1 D.1: канал спит вместе с днём проекта — `{project_id, since_ts}` или `None` (серая пометка).
        account["sleep"] = conn_sleep_mod.asleep(conn, account["id"], now_ms)

    dead_letter: dict[int, int] = {}
    for account in accounts:
        total = 0
        for schema in ("main", "work"):
            total += conn.execute(
                _sql(schema, "SELECT COUNT(*) FROM {schema}.dead_letter WHERE account_id = ?"),
                (account["id"],),
            ).fetchone()[0]
        dead_letter[account["id"]] = total

    pending = unknown = approval = 0
    for schema in ("main", "work"):
        pred, pred_params = _channel_scope(conn, principal, "c")
        row = conn.execute(
            _sql(
                schema,
                "SELECT "
                "  SUM(CASE WHEN o.status IN ('pending_approval', 'queued', 'sending') THEN 1 ELSE 0 END), "
                "  SUM(CASE WHEN o.status = 'unknown' THEN 1 ELSE 0 END), "
                "  SUM(CASE WHEN o.status = 'pending_approval' THEN 1 ELSE 0 END) "
                "FROM {schema}.outbox o JOIN {schema}.channel c ON c.id = o.channel_id WHERE ({pred})",
                pred=pred,
            ),
            pred_params,
        ).fetchone()
        pending += row[0] or 0
        unknown += row[1] or 0
        approval += row[2] or 0

    max_seq = conn.execute("SELECT value FROM main.seq_counter WHERE id = 1").fetchone()[0]
    return {
        "accounts": accounts,
        "dead_letter": dead_letter,
        "outbox_pending": pending,  # всё, что ещё не ушло: pending_approval + queued + sending
        "outbox_approval": approval,  # только ждущие решения человека (счётчик верхней полосы UI)
        "outbox_unknown": unknown,
        "max_seq": max_seq,
        "now": now_ms,
    }


def get_healthz(conn, now_ms: int) -> dict:
    """БЕЗ авторизации: только {ok, max_seq, now} — ни одного текста, ни одного идентификатора сущности."""
    max_seq = conn.execute("SELECT value FROM main.seq_counter WHERE id = 1").fetchone()[0]
    return {"ok": True, "max_seq": max_seq, "now": now_ms}


# --- Для пишущих ручек (этап 4b): видимость ДО записи, поиск кандидатов канала --------------------------


def channel_visible(conn, principal: Principal, channel_id: int) -> int | None:
    """`project_id` канала (может быть `None` — «Неразобранное»), если канал существует и виден принципалу;
    иначе `NotFound` — маршрут переводит её в 404 без уточнения, чего не хватает (не существует или чужой).
    """
    schema = db_of(channel_id)
    pred, pred_params = _scope_predicate("project_id", principal)
    row = conn.execute(
        _sql(schema, "SELECT project_id FROM {schema}.channel WHERE id = ? AND ({pred})", pred=pred),
        [channel_id, *pred_params],
    ).fetchone()
    if row is None:
        raise NotFound(f"канал {channel_id} не найден")
    _deny_service_bot(conn, principal, schema, channel_id)
    return row[0]


def outbox_visible(conn, principal: Principal, outbox_id: int) -> int:
    """`channel_id` исходящего, если строка существует и её канал виден принципалу; иначе `NotFound`."""
    schema = db_of(outbox_id)
    pred, pred_params = _channel_scope(conn, principal, "c")
    row = conn.execute(
        _sql(
            schema,
            "SELECT o.channel_id FROM {schema}.outbox o JOIN {schema}.channel c ON c.id = o.channel_id "
            "WHERE o.id = ? AND ({pred})",
            pred=pred,
        ),
        [outbox_id, *pred_params],
    ).fetchone()
    if row is None:
        raise NotFound(f"outbox {outbox_id} не найден")
    return row[0]


def message_visible(conn, principal: Principal, message_id: int, *, include_hidden: bool = False) -> int:
    """Ф8-5 A2.3: `channel_id` сообщения, если оно есть и его канал виден принципалу; иначе `NotFound`.
    Ф8-4: удалённое у себя в хабе — тоже `NotFound` (медиа, версии правок); `include_hidden` — только для
    самого `DELETE /messages/{id}` (повтор по уже скрытому — 204, П12)."""
    schema = db_of(message_id)
    pred, pred_params = _scope_predicate("c.project_id", principal)
    row = conn.execute(
        _sql(
            schema,
            "SELECT m.channel_id FROM {schema}.message m JOIN {schema}.channel c ON c.id = m.channel_id "
            "WHERE m.id = ? AND {hidden} AND ({pred})",
            pred=pred,
            hidden="1=1" if include_hidden else "m.hidden = 0",
        ),
        [message_id, *pred_params],
    ).fetchone()
    if row is None:
        raise NotFound(f"сообщение {message_id} не найдено")
    _deny_service_bot(conn, principal, schema, row[0])
    return row[0]


def message_edits(conn, principal: Principal, message_id: int) -> list[dict]:
    """Ф8-4 C3: `GET /messages/{id}/edits` — прежние версии сообщения от старой к новой (`replaced_ts` —
    когда версию сменила правка, `text`, `entities_json`). Видимость — `message_visible` (скоуп проекта через
    канал, скрытое — `NotFound`)."""
    message_visible(conn, principal, message_id)
    schema = db_of(message_id)
    rows = conn.execute(
        _sql(
            schema,
            "SELECT replaced_ts, text, entities_json FROM {schema}.message_edit WHERE message_id = ? "
            "ORDER BY id",
        ),
        (message_id,),
    ).fetchall()
    return [
        {"replaced_ts": replaced_ts, "text": text, "entities_json": entities_json}
        for replaced_ts, text, entities_json in rows
    ]


class MediaFile(NamedTuple):
    account_id: int
    sha256: str
    detected: str | None
    name: str | None


def media_file(conn, principal: Principal, media_id: int) -> MediaFile:
    """Ф8-5 A3: `GET /media/{id}` — скоуп проекта через сообщение -> канал, в самом SQL; чужое, несуществующее
    и не `ready` — одинаково `NotFound` (404), различать нельзя."""
    schema = db_of(media_id)
    pred, pred_params = _channel_scope(conn, principal, "c")
    row = conn.execute(
        _sql(
            schema,
            "SELECT md.account_id, md.sha256, md.detected, md.name FROM {schema}.media md "
            "JOIN {schema}.message m ON m.id = md.message_id JOIN {schema}.channel c ON c.id = m.channel_id "
            # Ф8-4: скрытое сообщение (его строк `media` и так нет — `hiding.py`) закрыто и явно.
            "WHERE md.id = ? AND md.status = 'ready' AND md.sha256 IS NOT NULL AND m.hidden = 0 AND ({pred})",
            pred=pred,
        ),
        [media_id, *pred_params],
    ).fetchone()
    if row is None:
        raise NotFound(f"медиа {media_id} не найдено")
    return MediaFile(*row)


def media_meta(conn, principal: Principal, media_id: int) -> dict:
    """Ф22-1 B1 (+П1, П3): описание вложения для сессии — тот же предикат проекта и `m.hidden = 0`, что у
    `media_file`, но без условия `ready`. Чужое, скрытое, несуществующее — одинаково `NotFound` (404).
    `name` — как в БД (от отправителя), чистит MCP; `size` — от источника, `stored_size` — фактический."""
    schema = db_of(media_id)
    pred, pred_params = _channel_scope(conn, principal, "c")
    row = conn.execute(
        _sql(
            schema,
            "SELECT md.id, md.status, md.size, md.stored_size, md.detected, md.name, c.project_id, "
            "m.channel_id, md.message_id, md.not_before_ts FROM {schema}.media md "
            "JOIN {schema}.message m ON m.id = md.message_id JOIN {schema}.channel c ON c.id = m.channel_id "
            "WHERE md.id = ? AND m.hidden = 0 AND ({pred})",
            pred=pred,
        ),
        [media_id, *pred_params],
    ).fetchone()
    if row is None:
        raise NotFound(f"медиа {media_id} не найдено")
    keys = (
        "id",
        "status",
        "size",
        "stored_size",
        "detected",
        "name",
        "project_id",
        "channel_id",
        "message_id",
        "not_before_ts",
    )
    return dict(zip(keys, row, strict=True))


# Пустой/односимвольный запрос отдал бы токену все видимые каналы с последними сообщениями одним вызовом.
RESOLVE_MIN_QUERY = 2
RESOLVE_MAX_CANDIDATES = 50

_RESOLVE_TEMPLATE = (
    "SELECT c.id, c.project_id, a.source, c.kind, c.title, c.first_seen_ts, c.my_msg_count, "
    "c.title_history_json, c.stage "
    "FROM {schema}.channel c JOIN main.account a ON a.id = c.account_id "
    "WHERE c.title IS NOT NULL AND ({pred}) AND {visible} {extra}"
)

_RESOLVE_RECENT_TEMPLATE = (
    "SELECT m.id, m.ts, m.is_me, m.text, o.author_role, o.auto_role "  # Ф39 B3: роль сессии-автора
    "FROM {schema}.message m LEFT JOIN {schema}.outbox o ON o.id = m.outbox_id "
    "WHERE m.channel_id = ? AND m.deleted = 0 AND m.hidden = 0 "
    "ORDER BY m.ts DESC, m.ext_sort DESC, m.id DESC LIMIT 3"
)


def resolve_channels(conn, principal: Principal, *, query: str, project_id: int | None) -> list[dict]:
    """Поиск канала по подстроке названия (без учёта регистра) для ручных решений при разборе неизвестных
    исходящих. БЕЗ ранжирования и без «лучшего» кандидата — порядок строго по `id`; фильтр проекта в SQL.

    Подстрока сравнивается в Python (`str.lower()`), а не в SQL: встроенные `LOWER()`/`UPPER()` SQLite
    складывают регистр только для ASCII, кириллица бы не находилась без учёта регистра. Проектный скоуп при
    этом всё равно накладывается в SQL — из БД читаются только строки, уже видимые принципалу.
    """
    needle = query.strip().lower()
    if len(needle) < RESOLVE_MIN_QUERY:
        return []
    pred, pred_params = _channel_scope(conn, principal, "c")
    extra_filter = ""
    params: list = list(pred_params)
    if project_id is not None:
        if project_id in (UNSORTED_PROJECT_ID, ALL_PROJECTS_ID):
            _check_pseudo_project(principal, project_id)
            if project_id == UNSORTED_PROJECT_ID:
                extra_filter += " AND c.project_id IS NULL"
        else:
            if not _project_exists(conn, project_id, principal):
                raise NotFound(f"проект {project_id} не существует для этого токена")
            extra_filter += " AND c.project_id = ?"
            params.append(project_id)

    candidates = []
    for schema in ("main", "work"):
        visible = threads_mod.channel_visible_sql(schema, "c")
        rows = conn.execute(
            _sql(schema, _RESOLVE_TEMPLATE, pred=pred, extra=extra_filter, visible=visible), params
        ).fetchall()
        for cid, pid, source, kind, title, first_seen_ts, my_msg_count, title_history_json, stage in rows:
            if needle not in title.lower():
                continue
            recent_rows = conn.execute(_sql(schema, _RESOLVE_RECENT_TEMPLATE), (cid,)).fetchall()
            candidates.append(
                {
                    "id": cid,
                    "project_id": pid,
                    "source": source,
                    "kind": kind,
                    "title": title,
                    "first_seen_ts": first_seen_ts,
                    "my_msg_count": my_msg_count,
                    "title_history_json": title_history_json,
                    "stage": stage,  # Ф34-1 A5: стадия в строке `hub_find_channel`
                    "recent_messages": [
                        {
                            "id": mid,
                            "ts": ts,
                            "is_me": bool(is_me),
                            "text": text,
                            "outbox_author_role": author_role,
                            "outbox_auto_role": auto_role,
                        }
                        for mid, ts, is_me, text, author_role, auto_role in recent_rows
                    ],
                }
            )
    candidates.sort(key=lambda c: c["id"])
    return candidates[:RESOLVE_MAX_CANDIDATES]


# --- Для /sync и /stream (этап 4b): единый поток изменений по seq, обе схемы ------------------------------


class Changes(NamedTuple):
    messages: list[dict]
    channels: list[dict]
    max_seq: int
    more: bool
    # Ф17-1 П6: заявки на вступление в группы и каналы Telegram (скоуп проекта).
    joins: tuple | list = ()
    # Ф17-2 A7: заявки на выход из групп и каналов Telegram (скоуп проекта; без проекта — П12).
    leaves: tuple | list = ()
    # Ф34-2 П9: напоминания по беседам (скоуп проекта напоминания; название и заметка — по видимости беседы).
    reminders: tuple | list = ()
    # Ф35-2 решение 6: сообщения агентов (с текстом) — ТОЛЬКО UI-сессии; остальным — всегда пусто.
    peer_msgs: tuple | list = ()
    # Ф38-2 П6: заявки на правило раскладки (скоуп проекта-цели; названия бесед и папок — только тому, кто
    # видит все проекты, П1).
    rule_requests: tuple | list = ()
    # Ф41-2б (3.3, П12): постоянные сессии хаба и их запросы (скоуп проекта сессии; `engine_session_id` —
    # только UI-сессии).
    hub_sessions: tuple | list = ()
    session_requests: tuple | list = ()
    # Ф41-5б (5.1): заявки агентов (перенос беседы, напарник) — ТОЛЬКО UI-сессии: в них задача и
    # причина (чужой ввод); остальным — всегда пусто.
    agent_requests: tuple | list = ()
    # Ф44-1c П8: нажатия кнопок ботов (с ответом бота) — ТОЛЬКО UI-сессии; остальным — всегда пусто.
    presses: tuple | list = ()
    # Ф54-1 П13: заявки «нужен человек» (с текстом автора) — ТОЛЬКО UI-сессии; остальным — всегда пусто.
    human_requests: tuple | list = ()
    # Ф62-6: операции над группами WhatsApp (скоуп проекта; без проекта — только UI и профиль без списка).
    group_ops: tuple | list = ()


_CHANGES_MESSAGE_TEMPLATE = (
    "SELECT m.id, m.seq, m.first_seq, m.channel_id, m.ext_id, m.ext_sort, m.thread_ext_id, "
    "m.thread_broadcast, m.purged, "
    "m.reply_to_ext_id, m.grouped_id, m.is_me, m.kind, m.text, m.entities_json, m.attachments_json, "
    "m.forward_json, m.mail_to_json, m.mentions_me, m.refs_json, m.reactions_json, m.ts, m.edited_ts, "
    "m.deleted, "
    "m.outbox_id, ct.id, ct.name, ct.handle, o.status, "
    "o.paced, CASE WHEN o.status = 'queued' THEN o.not_before_ts END, o.send_at_ts, o.cancel_if_reply, "
    "{unread}, {attention}, "
    "m.hidden, "
    "(SELECT COUNT(*) FROM {schema}.message_edit mev WHERE mev.message_id = m.id), "
    "m.mentions_broadcast, m.is_bot, m.bulk, m.views_json, o.auto_role, o.author_role, m.buttons_json, "
    "m.rule_hits, m.rule_attention, "
    "c.project_id "
    "FROM {schema}.message m "
    "JOIN {schema}.channel c ON c.id = m.channel_id "
    "LEFT JOIN main.project cp ON cp.id = c.project_id "
    "LEFT JOIN {schema}.contact ct ON ct.id = m.sender_contact_id "
    "LEFT JOIN {schema}.outbox o ON o.id = m.outbox_id "
    "LEFT JOIN {schema}.thread ut ON ut.channel_id = m.channel_id AND ut.ext_id = m.thread_ext_id "
    "WHERE m.seq > ? AND m.seq <= ? AND ({pred}) "
    "ORDER BY m.seq ASC LIMIT ?"
)

_CHANGES_CHANNEL_TEMPLATE = (
    "SELECT c.id, c.ext_id, c.kind, c.title, c.tier, c.muted, c.archived, c.map_reason, c.last_msg_ts, "
    "c.my_msg_count, c.first_seen_ts, c.project_id, c.seq, c.last_read_seq, a.source, "
    "{mode}, c.attention, {auto}, NOT {visible}, c.meta_json, c.peer_read_sort, {draft_state}, c.map_source, "
    "c.stage, c.stage_note, c.stage_ts, "  # Ф34-1 П5
    "c.account_id "  # Ф62-6: аккаунт беседы — как в списке и `GET /channels/{id}`
    "FROM {schema}.channel c JOIN main.account a ON a.id = c.account_id "
    "LEFT JOIN main.project pj ON pj.id = c.project_id "
    "WHERE c.seq > ? AND c.seq <= ? AND ({pred}) ORDER BY c.seq ASC LIMIT ?"
)


def _channel_change_dict(conn, cid: int, row, unread_react: int, *, full: bool = False) -> dict:
    (
        _cid,
        ext_id,
        kind,
        title,
        tier,
        muted,
        archived,
        map_reason,
        last_msg_ts,
        my_msg_count,
        first_seen_ts,
        project_id,
        seq,
        last_read_seq,
        source,
        attention,
        attention_manual,
        attention_auto,
        hidden,
        meta_json,
        peer_read_sort,
        draft_state,
        map_source,
        stage_name,
        stage_note,
        stage_ts,
        account_id,
    ) = row
    unread_top, unread_threads = unread_breakdown(conn, cid)
    return {
        "id": cid,
        "ext_id": ext_id,
        "kind": kind,
        "title": title,
        "tier": tier,
        "muted": bool(muted),
        "archived": bool(archived),
        "map_reason": map_reason,
        "map_source": map_source,
        "last_msg_ts": last_msg_ts,
        "my_msg_count": my_msg_count,
        "first_seen_ts": first_seen_ts,
        "project_id": project_id,
        "seq": seq,
        "last_read_seq": last_read_seq,
        "unread": unread_top + unread_threads,
        "unread_top": unread_top,
        "unread_threads": unread_threads,
        **_thread_nav(conn, cid, last_msg_ts),  # Ф66-5
        "source": source,
        "account_id": account_id,
        **_attention_fields(attention, attention_manual, attention_auto),
        "unread_react": unread_react,
        # Ф8-4 B3/П2: беседа удалена владельцем в хабе — UI убирает её из списка (и закрывает открытую).
        "hidden": bool(hidden),
        "peer_read_sort": peer_read_sort,
        **_reply_fields(meta_json, draft_state, full=full),
        **_stage_fields(stage_name, stage_note, stage_ts),
        "autosend": autosend_mod.roles(conn, cid),  # Ф37-1 A2
        **_muted_by_fields(conn, db_of(cid), cid),  # Ф99-2
    }


def changes_since(conn, principal: Principal, *, since: int, limit: int) -> Changes:
    """Сообщения и каналы с `seq > since` из обоих файлов, единый порядок по `seq`, общий `limit` на
    объединённый поток. Скоуп проекта — в SQL (сообщения — через JOIN с каналом), постфильтрации нет.

    Берём по `limit + 1` строк из каждого из 4 источников (main/work x message/channel), уже отсортированных
    по `seq` в SQL: k наименьших элементов объединения N отсортированных последовательностей всегда лежат
    среди первых k элементов каждой из них — значит слияние верхушек размера `limit + 1` даёт истинные
    `limit + 1` наименьших элементов всего объединения, откуда корректно берутся страница и флаг `more`.

    **Водяной знак.** WAL не коммитит два файла атомарно, а снимок каждого файла читатель берёт лениво, при
    первом обращении. Без защиты возможно: снимок `work` снят до коммита seq 10, снимок `main` — уже после
    следующей транзакции с seq 11; клиент получает 11, двигает курсор, и 10 потерян навсегда. Поэтому вся
    выборка идёт в одной читающей транзакции, ПЕРВЫМ читается `main.seq_counter` (фиксирует снимок `main`),
    и наружу не отдаётся ничего с `seq` выше него. Снимок `work` берётся позже снимка `main`, а писатель один
    и транзакции идут строго по очереди — значит всё, что `<= watermark`, в `work` либо уже видно, либо
    принадлежит последней, ещё не докоммиченной транзакции: тогда курсор просто не продвинется за неё.
    """
    own_tx = not conn.in_transaction
    if own_tx:
        conn.execute("BEGIN")
    try:
        return _changes_since(conn, principal, since=since, limit=limit)
    finally:
        if own_tx:
            conn.execute("ROLLBACK")


def _changes_since(conn, principal: Principal, *, since: int, limit: int) -> Changes:
    watermark = conn.execute("SELECT value FROM main.seq_counter WHERE id = 1").fetchone()[0]
    fetch_limit = limit + 1
    items: list[tuple[int, int, str, object]] = []  # (seq, tie-break, "message"/"channel", payload)
    tie = 0

    for schema in ("main", "work"):
        pred, pred_params = _channel_scope(conn, principal, "c")
        rows = conn.execute(
            _sql(
                schema,
                _CHANGES_MESSAGE_TEMPLATE,
                pred=pred,
                unread=_CHANGES_UNREAD,
                attention=_CHANGES_ATTENTION,
            ),
            [since, watermark, *pred_params, fetch_limit],
        ).fetchall()
        for row in rows:
            items.append((row[1], tie, "message", row))
            tie += 1

    for schema in ("main", "work"):
        pred, pred_params = _channel_scope(conn, principal, "c")
        rows = conn.execute(
            _sql(
                schema,
                _CHANGES_CHANNEL_TEMPLATE,
                pred=pred,
                mode=attention_mod.channel_mode_sql("c", "pj"),
                auto=attention_mod.channel_mode_sql("c", "pj", manual=False),
                visible=threads_mod.channel_visible_sql(schema, "c"),
                draft_state=drafts_mod.draft_state_sql(schema, "c"),
            ),
            [since, watermark, *pred_params, fetch_limit],
        ).fetchall()
        for row in rows:
            items.append((row[12], tie, "channel", row))
            tie += 1

    # Ф41-2б (3.3, П12): постоянные сессии хаба и их запросы — в том же потоке по `seq` (импорт внутри:
    # `held_work` сам опирается на этот модуль).
    from . import held_work as held_work_mod

    for seq, kind, payload in held_work_mod.sync_items(
        conn, principal, since=since, watermark=watermark, limit=fetch_limit
    ):
        items.append((seq, tie, kind, payload))
        tie += 1

    # Ф17-1 П6: заявки на вступление — в том же потоке по `seq` (скоуп проекта — в SQL).
    for join_item in join_mod.changes(conn, principal, since=since, watermark=watermark, limit=fetch_limit):
        items.append((join_item["seq"], tie, "join", join_item))
        tie += 1

    # Ф17-2 A7: заявки на выход — в том же потоке по `seq`.
    for leave_item in leave_mod.changes(conn, principal, since=since, watermark=watermark, limit=fetch_limit):
        items.append((leave_item["seq"], tie, "leave", leave_item))
        tie += 1

    # Ф62-6: операции над группами WhatsApp — в том же потоке по `seq` (импорт внутри: модуль тянет `subs`).
    from . import group_ops as group_ops_mod

    group_op_rows = group_ops_mod.changes(
        conn, principal, since=since, watermark=watermark, limit=fetch_limit
    )
    for group_op in group_op_rows:
        items.append((group_op["seq"], tie, "group_op", group_op))
        tie += 1

    # Ф34-2 П9: напоминания — в том же потоке по `seq`.
    for reminder in reminders_mod.changes(
        conn, principal, since=since, watermark=watermark, limit=fetch_limit
    ):
        items.append((reminder["seq"], tie, "reminder", reminder))
        tie += 1

    # Ф35-2 решение 6: сообщения агентов — только UI-сессии (иначе любая сессия прочла бы всю переписку
    # через `hub_changes`).
    if principal.is_ui:
        for peer_msg in peer_msgs_mod.changes(conn, since=since, watermark=watermark, limit=fetch_limit):
            items.append((peer_msg["seq"], tie, "peer_msg", peer_msg))
            tie += 1

    # Ф38-2 П6: заявки на правило раскладки — в том же потоке по `seq`.
    for rule_request in rule_requests_mod.changes(
        conn, principal, since=since, watermark=watermark, limit=fetch_limit
    ):
        items.append((rule_request["seq"], tie, "rule_request", rule_request))
        tie += 1

    # Ф41-5б (5.1): заявки агентов — только UI-сессии (импорт внутри: модуль опирается на этот).
    if principal.is_ui:
        from . import agent_requests as agent_requests_mod

        for agent_request in agent_requests_mod.changes(
            conn, principal, since=since, watermark=watermark, limit=fetch_limit
        ):
            items.append((agent_request["seq"], tie, "agent_request", agent_request))
            tie += 1

    # Ф44-1c П8: нажатия кнопок ботов — только UI-сессии: в строке ответ бота, а он — владельцу и автору
    # (сессия читает свои нажатия `GET /button-presses`). Импорт внутри: модуль тянет `autosend`.
    if principal.is_ui:
        from . import presses as presses_mod

        for press in presses_mod.changes(conn, since=since, watermark=watermark, limit=fetch_limit):
            items.append((press["seq"], tie, "press", press))
            tie += 1

    # Ф54-1 П13: заявки «нужен человек» — только UI-сессии (сессия читает заявки своей линии
    # `GET /human-requests`, ответчик-агент — по выключателю вида).
    if principal.is_ui:
        from . import human_requests as human_requests_mod

        for human in human_requests_mod.changes(conn, since=since, watermark=watermark, limit=fetch_limit):
            items.append((human["seq"], tie, "human_request", human))
            tie += 1

    items.sort(key=lambda item: (item[0], item[1]))
    more = len(items) > limit
    page = items[:limit]

    messages = []
    hidden_changes: list[dict] = []
    channels = []
    n_message_cols = len(_MESSAGE_COLUMNS)
    # Ф8-3 A5: `unread_react` каналов страницы — одним сгруппированным запросом на схему.
    channel_ids: dict[str, list[int]] = defaultdict(list)
    for _seq, _tie, kind, row in page:
        if kind == "channel":
            channel_ids[db_of(row[0])].append(row[0])
    react: dict[int, int] = {}
    for schema, ids in channel_ids.items():
        placeholders = ",".join("?" for _ in ids)
        react.update(_unread_react_map(conn, schema, f"c.id IN ({placeholders})", ids))
    joins = [row for _seq, _tie, kind, row in page if kind == "join"]
    leaves = [row for _seq, _tie, kind, row in page if kind == "leave"]
    reminders = [row for _seq, _tie, kind, row in page if kind == "reminder"]
    peer_msgs = [row for _seq, _tie, kind, row in page if kind == "peer_msg"]
    rule_requests = [row for _seq, _tie, kind, row in page if kind == "rule_request"]
    hub_sessions = [row for _seq, _tie, kind, row in page if kind == "hub_session"]
    session_requests = [row for _seq, _tie, kind, row in page if kind == "session_request"]
    agent_requests = [row for _seq, _tie, kind, row in page if kind == "agent_request"]
    presses = [row for _seq, _tie, kind, row in page if kind == "press"]
    human_requests = [row for _seq, _tie, kind, row in page if kind == "human_request"]
    group_ops = [row for _seq, _tie, kind, row in page if kind == "group_op"]
    for _seq, _tie, kind, row in page:
        if kind in (
            "join",
            "leave",
            "reminder",
            "peer_msg",
            "rule_request",  # Ф38-2 П6: иначе словарь заявки ушёл бы в `_channel_change_dict`
            "hub_session",  # Ф41-2б: то же для сессий хаба и их запросов
            "session_request",
            "agent_request",  # Ф41-5б
            "press",  # Ф44-1c
            "human_request",  # Ф54-1
            "group_op",  # Ф62-6
        ):
            continue
        if kind == "message":
            d = dict(zip(_MESSAGE_COLUMNS, row[:n_message_cols], strict=True))
            if d["hidden"]:
                hidden_changes.append(_hidden_change(row[:n_message_cols], row[n_message_cols]))
            else:
                messages.append(_message_row_to_dict(d, row[n_message_cols]))
        else:
            channels.append(
                _channel_change_dict(conn, row[0], row, react.get(row[0], 0), full=principal.is_ui)
            )

    # Раздел E: `/sync`/SSE несут `thread` в словаре сообщения-ответа тоже (не только список канала) — сводки
    # одним сгруппированным запросом НА СХЕМУ (main/work могут быть перемешаны в одной странице `/sync`).
    by_schema: dict[str, list[dict]] = defaultdict(list)
    for m in messages:
        by_schema[db_of(m["channel_id"])].append(m)
    for schema, msgs in by_schema.items():
        _attach_thread_summaries(conn, schema, msgs)
        media_mod.attach_media(conn, schema, msgs)  # Ф8-5 A3: `/sync` и SSE несут `media` тоже
        msg_ops_mod.attach_ops(conn, schema, msgs)  # Ф14-8 A2: поле `op` (правка/удаление у всех)
        peer_flood_mod.attach_held(conn, msgs)  # Ф69-1: `outbox_held`
        reactions_mod.attach_can_react(conn, schema, msgs)  # Ф15-1 A6: `can_react` писем
    mention_names_mod.attach_messages(conn, messages)  # Ф27-1 B: одна выборка справочника на ответ

    # Ф8-4 B2/П3: заглушки скрытых — в общем потоке по `seq` (порядок изменений важен для UI).
    if hidden_changes:
        messages = sorted([*messages, *hidden_changes], key=lambda m: m["seq"])

    max_seq = page[-1][0] if page else since
    return Changes(
        messages=messages,
        channels=channels,
        max_seq=max_seq,
        more=more,
        joins=joins,
        leaves=leaves,
        reminders=reminders,
        peer_msgs=peer_msgs,
        rule_requests=rule_requests,
        hub_sessions=hub_sessions,
        session_requests=session_requests,
        agent_requests=agent_requests,
        presses=presses,
        human_requests=human_requests,
        group_ops=group_ops,
    )


# --- Ф11-1 (docs/architecture.md): ожидание по одной беседе, курсор — глобальный seq -------

_THREAD_ROWS_PRED = "(m.thread_ext_id = ? OR m.ext_id = ?)"


class ChannelSeqPage(NamedTuple):
    """Строки одной беседы с `seq > after` по возрастанию `seq`. `messages` — видимые, в форме `/sync`
    (со сводками тредов и `media`); `last_seq` — seq последней строки страницы, СКРЫТЫЕ у себя тоже (они в
    выдачу не идут, но курсор за них двигается); `None` — строк нет."""

    messages: list[dict]
    last_seq: int | None
    more: bool


def channel_max_seq(conn, principal: Principal, channel_id: int, *, thread: str | None = None) -> int:
    """Курсор ожидания беседы (Ф11-1, решение 1): `MAX(seq)` её строк, скрытые у себя тоже; с `thread` —
    строк треда и родителя. Пустая беседа — 0. Невидимая принципалу — `NotFound` (та же проверка, что у
    `list_messages`)."""
    channel_visible(conn, principal, channel_id)
    schema = db_of(channel_id)
    params: list = [channel_id]
    thread_pred = "1=1"
    if thread is not None:
        thread_pred = _THREAD_ROWS_PRED
        params.extend([thread, thread])
    row = conn.execute(
        _sql(
            schema,
            "SELECT COALESCE(MAX(m.seq), 0) FROM {schema}.message m WHERE m.channel_id = ? AND {thread}",
            thread=thread_pred,
        ),
        params,
    ).fetchone()
    return row[0]


def channel_messages_after(
    conn, principal: Principal, channel_id: int, *, after: int, thread: str | None, limit: int
) -> ChannelSeqPage:
    """Ф11-1 A1: строки беседы с `seq > after` (с `thread` — только тред и его родитель) — тот же SELECT и то
    же преобразование, что у `/sync` (`_CHANGES_MESSAGE_TEMPLATE`), только ограниченный одной беседой.
    Видимость беседы — `channel_visible` (как `list_messages`), скоуп ещё и в самом SQL. Водяной знак
    `seq_counter` не нужен (решение 1: беседа лежит в одном файле, писатель один), но и не мешает: берём его,
    чтобы шаблон `/sync` был общим без правок."""
    channel_visible(conn, principal, channel_id)
    schema = db_of(channel_id)
    watermark = conn.execute("SELECT value FROM main.seq_counter WHERE id = 1").fetchone()[0]
    scope, scope_params = _channel_scope(conn, principal, "c")
    pred = f"m.channel_id = ? AND ({scope})"
    params: list = [after, watermark, channel_id, *scope_params]
    if thread is not None:
        pred += f" AND {_THREAD_ROWS_PRED}"
        params.extend([thread, thread])
    params.append(limit + 1)
    rows = conn.execute(
        _sql(
            schema,
            _CHANGES_MESSAGE_TEMPLATE,
            pred=pred,
            unread=_CHANGES_UNREAD,
            attention=_CHANGES_ATTENTION,
        ),
        params,
    ).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    n_message_cols = len(_MESSAGE_COLUMNS)
    messages = []
    for row in rows:
        d = dict(zip(_MESSAGE_COLUMNS, row[:n_message_cols], strict=True))
        if not d["hidden"]:
            messages.append(_message_row_to_dict(d, row[n_message_cols]))
    _attach_thread_summaries(conn, schema, messages)
    media_mod.attach_media(conn, schema, messages)
    msg_ops_mod.attach_ops(conn, schema, messages)  # Ф14-8 A2: поле `op` (правка/удаление у всех)
    peer_flood_mod.attach_held(conn, messages)  # Ф69-1: `outbox_held`
    reactions_mod.attach_can_react(conn, schema, messages)  # Ф15-1 A6: `can_react` писем
    return ChannelSeqPage(messages, rows[-1][1] if rows else None, more)


# --- Карточки дежурного агента (спека 7b, A2, П5, К3) ------------------------------------------------------

AGENT_ITEM_STATUSES = frozenset({"new", "seen", "done", "dismissed"})
AGENT_ITEM_LABELS = frozenset({"urgent", "reply", "fyi", "noise"})
AGENT_ITEM_OPEN = "open"  # `GET /agent/items?status=open` — `new` и `seen` вместе (Ф37-2 B2)

_AGENT_ITEM_REPLY_PRED = threads_mod.is_reply_sql("lm.thread_ext_id", "lm.ext_id")
_AGENT_ITEMS_TEMPLATE = (
    "SELECT a.id, a.channel_id, a.label, a.summary, a.action, a.message_ids_json, a.last_message_id, "
    "a.status, a.provider, a.model, a.created_ts, a.updated_ts, a.purged, a.covered_seq, c.project_id, "
    "c.title, acc.source, "
    "c.kind, lm.ts, lm.ext_sort, "
    # П5: «ответил» / «прочитал» — одно определение с записью карточки (Ф11-2 A1, `agent_items`).
    + agent_items_mod.ANSWERED_SQL
    + ", "
    + agent_items_mod.READ_SQL
    + ", "
    # Ф8-3 C2/П8: тред последнего сообщения карточки (ответ, не broadcast) — hub-wake сверяет присутствие:
    # ответ «виден», только если открыта панель ЭТОГО треда.
    "CASE WHEN {reply_pred} AND lm.thread_broadcast = 0 THEN lm.thread_ext_id END, "
    # Ф38-1 П8: аккаунт беседы — В КОНЦЕ (сортировка `list_agent_items` идёт по позициям столбцов).
    "c.account_id "
    "FROM {schema}.agent_item a "
    "JOIN {schema}.channel c ON c.id = a.channel_id "
    "JOIN main.account acc ON acc.id = c.account_id "
    "JOIN {schema}.message lm ON lm.id = a.last_message_id "
    "LEFT JOIN {schema}.thread t ON t.channel_id = lm.channel_id AND t.ext_id = lm.thread_ext_id "
    # Ф8-4 П3/П8: карточка, чьё последнее сообщение удалено у себя в хабе, агентам и UI не выдаётся (открытые
    # такие карточки скрытие и так закрывает — П4).
    "WHERE lm.hidden = 0 AND ({pred}) {extra} "
    "ORDER BY a.updated_ts DESC, a.id DESC LIMIT ?"
)


def _agent_item_row(row) -> dict:
    (
        item_id,
        channel_id,
        label,
        summary,
        action,
        message_ids_json,
        last_message_id,
        status,
        provider,
        model,
        created_ts,
        updated_ts,
        purged,
        covered_seq,
        project_id,
        channel_title,
        source,
        kind,
        last_message_ts,
        last_message_ext_sort,
        answered,
        read,
        last_message_thread,
        account_id,
    ) = row
    try:
        message_ids = [int(x) for x in json.loads(message_ids_json)]
    except ValueError, TypeError:
        message_ids = []
    return {
        "id": item_id,
        "channel_id": channel_id,
        "label": label,
        "summary": summary,
        "action": action,
        "message_ids": message_ids,
        "last_message_id": last_message_id,
        "last_message_ts": last_message_ts,
        "last_message_ext_sort": last_message_ext_sort,
        "status": status,
        "provider": provider,
        "model": model,
        "created_ts": created_ts,
        "updated_ts": updated_ts,
        "purged": bool(purged),
        # К3 (после приёмки, F1): максимум `first_seq` покрытых сообщений — агент по нему отсекает уже
        # разобранных кандидатов (по порядку поступления в хаб, не по времени источника).
        "covered_seq": covered_seq,
        "project_id": project_id,
        "channel_title": channel_title,
        "source": source,
        "kind": kind,
        "answered": bool(answered),
        "read": bool(read),
        "last_message_thread": last_message_thread,
        # Ф38-1 п.4: аккаунт беседы — hub-wake пропускает подписки, чей фильтр по аккаунтам его не слышит.
        "account_id": account_id,
    }


def list_agent_items(
    conn,
    principal: Principal,
    *,
    status: str,
    project_id: int | None,
    label: str | None,
    channel_id: int | None,
    limit: int,
) -> list[dict]:
    """`GET /agent/items` (спека 7b, A2): карточки по ОБОИМ файлам, свежие первыми по `updated_ts`. `status` —
    один из статусов или `any` (К3); `project` — как в `/inbox` (`-1` — по `sees_unsorted`, `-2` — только UI);
    проектный скоуп — в SQL через `channel.project_id`, как везде."""
    pred, pred_params = _channel_scope(conn, principal, "c")
    extra = ""
    params: list = list(pred_params)
    if status == AGENT_ITEM_OPEN:
        extra += " AND a.status IN ('new', 'seen')"  # Ф37-2 B2: открытые — одним запросом
    elif status != "any":
        extra += " AND a.status = ?"
        params.append(status)
    if project_id is not None:
        if project_id in (UNSORTED_PROJECT_ID, ALL_PROJECTS_ID):
            _check_pseudo_project(principal, project_id)
            if project_id == UNSORTED_PROJECT_ID:
                extra += " AND c.project_id IS NULL"
        else:
            if not _project_exists(conn, project_id, principal):
                raise NotFound(f"проект {project_id} не существует для этого токена")
            extra += " AND c.project_id = ?"
            params.append(project_id)
    if label is not None:
        extra += " AND a.label = ?"
        params.append(label)
    if channel_id is not None:
        extra += " AND a.channel_id = ?"
        params.append(channel_id)

    rows: list = []
    for schema in ("main", "work"):
        if channel_id is not None and db_of(channel_id) != schema:
            continue
        rows.extend(
            conn.execute(
                _sql(
                    schema, _AGENT_ITEMS_TEMPLATE, pred=pred, extra=extra, reply_pred=_AGENT_ITEM_REPLY_PRED
                ),
                [*params, limit],
            ).fetchall()
        )
    rows.sort(key=lambda r: (r[11], r[0]), reverse=True)
    items = [_agent_item_row(r) for r in rows[:limit]]
    # Ф13-1 B4: последний сигнал карточки по живой подписке её проекта (`None` — нет).
    subs_mod.attach_signals(conn, items)
    # Ф14-5 (после ревью): последний сигнал сообщений беседы от подписки на беседу или режима `messages`.
    chan_subs_mod.attach_msg_signals(conn, items)
    return items


def agent_item_channel(conn, principal: Principal, item_id: int) -> int:
    """`channel_id` карточки, если она есть и её канал виден принципалу; иначе `NotFound` (404)."""
    schema = db_of(item_id)
    pred, pred_params = _channel_scope(conn, principal, "c")
    row = conn.execute(
        _sql(
            schema,
            "SELECT a.channel_id FROM {schema}.agent_item a JOIN {schema}.channel c ON c.id = a.channel_id "
            "WHERE a.id = ? AND ({pred})",
            pred=pred,
        ),
        [item_id, *pred_params],
    ).fetchone()
    if row is None:
        raise NotFound(f"карточка {item_id} не найдена")
    return row[0]


# --- Токены: список для CLI-админа и проверка «перенос не расширяет видимость» (спека 7a, A6/П4) ----------


def list_bearer_tokens(conn) -> list[dict]:
    """`GET /admin/tokens` — только bearer-виды (`interactive`/`background`), без хешей."""
    rows = conn.execute(
        "SELECT id, name, kind, scopes, project_scope_json, expires_ts, revoked FROM main.api_token "
        "WHERE kind IN ('interactive', 'background') ORDER BY id"
    ).fetchall()
    return [
        {
            "id": tid,
            "name": name,
            "kind": kind,
            "scopes": (scopes or "").split(),
            "project_ids": json.loads(project_scope_json) if project_scope_json is not None else None,
            "expires_ts": expires_ts,
            "revoked": bool(revoked),
        }
        for tid, name, kind, scopes, project_scope_json, expires_ts, revoked in rows
    ]


def tokens_gaining_project(
    conn, *, source_project_id: int | None, target_project_id: int | None, now_ms: int
) -> list[dict]:
    """Спека 7a, П4: действующие bearer-токены со списком проектов, которым перенос канала из S в T ОТКРЫЛ бы
    его: T в списке, S — нет (`None` — «Неразобранное» — не входит ни в один список). Пустой список —
    перенос видимость проектных токенов не расширяет."""
    if target_project_id is None or target_project_id == source_project_id:
        return []
    rows = conn.execute(
        "SELECT id, name, project_scope_json FROM main.api_token "
        "WHERE kind IN ('interactive', 'background') AND revoked = 0 "
        "AND (expires_ts IS NULL OR expires_ts > ?) AND project_scope_json IS NOT NULL ORDER BY id",
        (now_ms,),
    ).fetchall()
    gaining = []
    for tid, name, project_scope_json in rows:
        ids = set(json.loads(project_scope_json))
        if target_project_id in ids and source_project_id not in ids:
            gaining.append({"id": tid, "name": name})
    return gaining


# --- Для внутреннего сокета: коннектор и CLI-админ (кусок 1a фазы 1) ---------------------------------------
#
# Закрывает долг «SQL в routes/internal.py» (`get_cursor`, `_watch_snapshot`), отмеченный в «Поправках» кусков
# 5a/7 — их SQL переехал сюда же, к остальным читающим запросам.


def connector_cursor(conn, account_id: int) -> dict | None:
    """Тот же запрос, что раньше жил прямо в `routes/internal.py::get_cursor`."""
    row = conn.execute("SELECT db FROM main.account WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        return None
    schema = row[0]
    cur = conn.execute(
        _sql(schema, "SELECT value, ts FROM {schema}.cursor WHERE account_id = ?"), (account_id,)
    ).fetchone()
    if cur is None:
        return None
    value, ts = cur
    return {"value": value, "ts": ts}


def connector_account(conn, account_id: int) -> dict | None:
    """`GET /account` коннектора (раздел C спеки куска 1a) — своего аккаунта, по `ConnectorPrincipal`."""
    row = conn.execute(
        "SELECT source, title, config_json, status, flood_until FROM main.account WHERE id = ?", (account_id,)
    ).fetchone()
    if row is None:
        return None
    source, title, config_json, status, flood_until = row
    config = json.loads(config_json) if config_json else None
    return {
        "account_id": account_id,
        "source": source,
        "title": title,
        "config": config,
        "status": status,
        "flood_until": flood_until,
    }


def _channel_own_id_space(meta_json: str | None) -> bool:
    if not meta_json:
        return False
    try:
        return bool(json.loads(meta_json).get("own_id_space", False))
    except ValueError, TypeError, AttributeError:
        return False


def connector_channel_heads(conn, account_id: int, since_ts: int | None = None) -> list[dict]:
    """`GET /channels/heads` (раздел C, доп. `max_ext_id` — раздел A спеки 2b) — опора сверки. `max_ext_sort`
    оставлен как есть для Telegram (максимум `ext_sort` среди строк `ext_id IS NOT NULL`, заготовки outbox не
    считаются). `max_ext_id` — ext_id строки с максимальной позицией среди `ext_id IS NOT NULL` И
    `top_level` (курсор `conversations.history(oldest=…)` Slack: ответы и заготовки его не двигают).

    Ф42-3 B: с `since_ts` (мс) в каждой строке ещё `project_id` (как хранится; «Неразобранное» — `None`) и
    `refill_lo_ext_sort` — позиция самого нового известного сообщения беседы с `ts < since_ts`, а если таких
    нет — самого старого известного; сообщений нет — `None`. Без параметра ответ прежний."""
    row = conn.execute("SELECT db FROM main.account WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        return []
    schema = row[0]

    channels = conn.execute(
        _sql(
            schema,
            "SELECT c.id, c.ext_id, c.kind, p.ext_id, c.meta_json, c.last_msg_ts, c.archived, c.project_id "
            "FROM {schema}.channel c LEFT JOIN {schema}.channel p ON p.id = c.parent_channel_id "
            # Ф12-1 A7: беседа-заготовка (временный `ext_id`) коннектору не видна.
            "WHERE c.account_id = ? AND {not_temp}",
            not_temp=drafts_mod.not_temp_sql("c"),
        ),
        (account_id,),
    ).fetchall()

    top_level = threads_mod.top_level_sql("thread_ext_id", "ext_id", "thread_broadcast")
    skip_bots = set(service_bot_mod.account_bot_ids(conn, account_id))
    result = []
    for channel_id, ext_id, kind, parent_ext_id, meta_json, last_msg_ts, archived, project_id in channels:
        max_ext_sort = conn.execute(
            _sql(
                schema,
                "SELECT MAX(ext_sort) FROM {schema}.message WHERE channel_id = ? AND ext_id IS NOT NULL",
            ),
            (channel_id,),
        ).fetchone()[0]
        max_ext_id = conn.execute(
            _sql(
                schema,
                "SELECT ext_id FROM {schema}.message WHERE channel_id = ? AND ext_id IS NOT NULL "
                "AND {top_level} ORDER BY ts DESC, ext_sort DESC, id DESC LIMIT 1",
                top_level=top_level,
            ),
            (channel_id,),
        ).fetchone()
        head = {
            "ext_id": ext_id,
            "kind": kind,
            "parent_ext_id": parent_ext_id,
            "own_id_space": _channel_own_id_space(meta_json),
            "max_ext_sort": max_ext_sort,
            "max_ext_id": max_ext_id[0] if max_ext_id is not None else None,
            "last_msg_ts": last_msg_ts,
            "archived": bool(archived),
        }
        if since_ts is not None:
            refill_lo = conn.execute(
                _sql(
                    schema,
                    "SELECT COALESCE("
                    "(SELECT MAX(ext_sort) FROM {schema}.message WHERE channel_id = ?1 "
                    "AND ext_id IS NOT NULL AND ts < ?2), "
                    "(SELECT MIN(ext_sort) FROM {schema}.message WHERE channel_id = ?1 "
                    "AND ext_id IS NOT NULL))",
                ),
                (channel_id, since_ts),
            ).fetchone()[0]
            head["project_id"] = project_id
            head["refill_lo_ext_sort"] = refill_lo
        if kind == "dm" and ext_id in skip_bots:
            head["skip"] = True
            skip_bots.discard(ext_id)
        result.append(head)
    # Ф54-2 (П2): личка с ботом хаба ядром отбрасывается — коннектору Telegram сказано её не грузить
    # (`skip`), и когда беседы в хабе нет вовсе (строка-заглушка без позиций).
    for bot in sorted(skip_bots):
        head = {
            "ext_id": bot,
            "kind": "dm",
            "parent_ext_id": None,
            "own_id_space": False,
            "max_ext_sort": None,
            "max_ext_id": None,
            "last_msg_ts": None,
            "archived": True,
            "skip": True,
        }
        if since_ts is not None:
            head["project_id"] = None
            head["refill_lo_ext_sort"] = None
        result.append(head)
    return result


def connector_channel_window(
    conn, account_id: int, channel_ext_id: str, limit: int, *, thread_ext_id: str | None = None
) -> list[str]:
    """`GET /channels/window` (кусок 1b, раздел «Сверка»; `?thread_ext_id=` — раздел A спеки 2b): `ext_id`
    НЕ удалённых сообщений канала этого аккаунта, по убыванию `(ts, ext_sort, ext_id)` — не больше `limit`.
    Без `thread_ext_id` — только `top_level` (Telegram не меняется — у него ответов нет; иначе сверка окна
    Slack против `conversations.history`, где ответов нет, пометила бы их все удалёнными). С `thread_ext_id`
    — окно ТОЛЬКО ОТВЕТОВ этого треда, родитель НИКОГДА (Поправки после opus-критика кода, E2: раньше
    родитель, пришедший с `thread_ext_id == ext_id`, попадал в окно — сверка через `conversations.replies`,
    которая видит только ответы, пометила бы такого родителя удалённым). Канал чужого/неизвестного
    аккаунта -> пустой список (сверка сама решит, что чат неизвестен)."""
    row = conn.execute("SELECT db FROM main.account WHERE id = ?", (account_id,)).fetchone()
    if row is None or drafts_mod.is_temp_ext_id(channel_ext_id):  # Ф12-1 A7
        return []
    schema = row[0]
    crow = conn.execute(
        _sql(schema, "SELECT id FROM {schema}.channel WHERE account_id = ? AND ext_id = ?"),
        (account_id, channel_ext_id),
    ).fetchone()
    if crow is None:
        return []
    channel_id = crow[0]

    if thread_ext_id is not None:
        is_reply_pred = threads_mod.is_reply_sql("thread_ext_id", "ext_id")
        rows = conn.execute(
            _sql(
                schema,
                "SELECT ext_id FROM {schema}.message WHERE channel_id = ? AND thread_ext_id = ? "
                "AND deleted = 0 AND ext_id IS NOT NULL AND {is_reply} "
                "ORDER BY ts DESC, ext_sort DESC, id DESC LIMIT ?",
                is_reply=is_reply_pred,
            ),
            (channel_id, thread_ext_id, limit),
        ).fetchall()
        return [r[0] for r in rows]

    # Тай-брейк по `id` (не `ext_id`) — БУКВАЛЬНО тот же запрос, что был до спеки 2b, плюс фильтр top_level:
    # раздел A требует «Telegram-окно не изменилось» (тест 7), а `id` — оригинальный тай-брейк этой ручки.
    top_level = threads_mod.top_level_sql("thread_ext_id", "ext_id", "thread_broadcast")
    rows = conn.execute(
        _sql(
            schema,
            "SELECT ext_id FROM {schema}.message WHERE channel_id = ? AND deleted = 0 "
            "AND ext_id IS NOT NULL AND {top_level} ORDER BY ts DESC, ext_sort DESC, id DESC LIMIT ?",
            top_level=top_level,
        ),
        (channel_id, limit),
    ).fetchall()
    return [r[0] for r in rows]


def connector_threads_heads(conn, account_id: int, since_ms: int, limit: int = 2000) -> list[dict]:
    """`GET /internal/v1/threads/heads?since_ms=` (раздел H спеки 2b) — треды своего аккаунта с
    `last_reply_ts >= since_ms` ИЛИ родителем моложе `since_ms`, не больше `limit`, свежие первыми.
    Инвариант для коннектора 2c (записать в спеку 2c): он догружает родителя любого треда, где есть ответ
    (`parent_known = false`)."""
    row = conn.execute("SELECT db FROM main.account WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        return []
    schema = row[0]

    rows = conn.execute(
        _sql(
            schema,
            "SELECT c.id, c.ext_id, t.ext_id, t.last_reply_ts, t.source_latest_reply_ext, t.subscribed, "
            "(p.id IS NOT NULL) "
            "FROM {schema}.thread t "
            "JOIN {schema}.channel c ON c.id = t.channel_id "
            "LEFT JOIN {schema}.message p ON p.channel_id = t.channel_id AND p.ext_id = t.ext_id "
            "WHERE c.account_id = ? AND {not_temp} "
            "AND (COALESCE(t.last_reply_ts, 0) >= ? OR (p.ts IS NOT NULL AND p.ts >= ?)) "
            "ORDER BY max(COALESCE(t.last_reply_ts, 0), COALESCE(p.ts, 0)) DESC "
            "LIMIT ?",
            not_temp=drafts_mod.not_temp_sql("c"),
        ),
        (account_id, since_ms, since_ms, limit),
    ).fetchall()

    result = []
    for row in rows:
        (
            channel_id,
            channel_ext_id,
            thread_ext_id,
            last_reply_ts,
            source_latest_reply_ext,
            subscribed,
            parent_known,
        ) = row
        max_reply_row = conn.execute(
            _sql(
                schema,
                "SELECT ext_id FROM {schema}.message WHERE channel_id = ? AND thread_ext_id = ? "
                "AND ext_id IS NOT NULL AND ext_id <> thread_ext_id "
                "ORDER BY ts DESC, ext_sort DESC, id DESC LIMIT 1",
            ),
            (channel_id, thread_ext_id),
        ).fetchone()
        result.append(
            {
                "channel_ext_id": channel_ext_id,
                "thread_ext_id": thread_ext_id,
                "max_reply_ext_id": max_reply_row[0] if max_reply_row is not None else None,
                "last_reply_ts": last_reply_ts,
                "source_latest_reply_ext": source_latest_reply_ext,
                "subscribed": bool(subscribed),
                "parent_known": bool(parent_known),
            }
        )
    return result


def account_meta_value(conn, account_id: int, key: str) -> dict | None:
    """`GET /admin/accounts/{id}/meta/{key}` — значение `account_meta` в файле ЭТОГО аккаунта."""
    row = conn.execute("SELECT db FROM main.account WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        return None
    schema = row[0]
    meta_row = conn.execute(
        _sql(schema, "SELECT value_json FROM {schema}.account_meta WHERE account_id = ? AND key = ?"),
        (account_id, key),
    ).fetchone()
    if meta_row is None:
        return None
    return {"key": key, "value": json.loads(meta_row[0])}


ACCOUNT_META_PARTS_MAX = 1000  # страховка от кривого заголовка: 1000 порций по 40 КиБ — с большим запасом


def account_meta_list(conn, account_id: int, name: str) -> list[dict]:
    """Ф14-2 A6 (П4): справочник аккаунта порциями — заголовок `<name>` = `{"parts": N, "count": M}`, порции
    `<name>:0` … `<name>:N-1` — списки. Собирает записи-словари по `parts`; лишние старые порции (от прошлой
    длинной публикации) не читаются; нет заголовка или он кривой — пусто; нет порции или она кривая — она
    пропускается. Коннектор шлёт порции ДО заголовка, так что заголовок не ссылается на недошедшие."""
    row = conn.execute("SELECT db FROM main.account WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        return []
    schema = row[0]

    def _value(key: str):
        meta_row = conn.execute(
            _sql(schema, "SELECT value_json FROM {schema}.account_meta WHERE account_id = ? AND key = ?"),
            (account_id, key),
        ).fetchone()
        if meta_row is None:
            return None
        try:
            return json.loads(meta_row[0])
        except ValueError, TypeError:
            return None

    header = _value(name)
    parts = header.get("parts") if isinstance(header, dict) else None
    if isinstance(parts, bool) or not isinstance(parts, int) or parts <= 0:
        return []
    items: list[dict] = []
    for index in range(min(parts, ACCOUNT_META_PARTS_MAX)):
        part = _value(f"{name}:{index}")
        if isinstance(part, list):
            items.extend(item for item in part if isinstance(item, dict))
    return items


def watch_snapshot(conn, now_ms: int) -> dict:
    """Тот же запрос, что раньше жил прямо в `routes/internal.py::_watch_snapshot`, плюс
    `connector_secret` на аккаунт — опора проблемы `account:<id>:silent` (раздел E спеки куска 1a):
    молчащий пульс интересен только у аккаунтов, за которыми стоит настоящий коннектор. `last_alert_ts` —
    кусок 1c, раздел «Алерт о новой сессии Telegram»: последний `audit(action='connector_alert')` этого
    аккаунта (id — в `detail_json`, своей колонки под него в `audit` нет); порог 24 ч проверяет `watch.py`.
    Ф51-1 A4: `last_error` — только у аккаунта в `auth_required` (причина отказа входа)."""
    account_rows = conn.execute(
        "SELECT a.id, a.source, a.title, a.status, a.last_ok_ts, a.flood_until, "
        "(cs.account_id IS NOT NULL), a.last_error, a.peer_flood_ts FROM main.account a "
        "LEFT JOIN main.connector_secret cs ON cs.account_id = a.id ORDER BY a.id"
    ).fetchall()

    alert_rows = conn.execute(
        "SELECT json_extract(detail_json, '$.account_id'), MAX(ts) FROM main.audit "
        "WHERE action = 'connector_alert' GROUP BY json_extract(detail_json, '$.account_id')"
    ).fetchall()
    last_alert_by_account = {int(aid): ts for aid, ts in alert_rows if aid is not None}

    accounts = [
        {
            "id": aid,
            "source": source,
            "title": title,
            "status": status,
            "last_ok_ts": last_ok_ts,
            "flood_until": flood_until,
            "has_connector_secret": bool(has_secret),
            "last_alert_ts": last_alert_by_account.get(aid),
            # Ф51-1 A4: причина отказа входа (её пишет каркас коннектора — вид отказа, без текстов и
            # адресов) — только у `auth_required`; у прочих статусов `last_error` сторожу не отдаётся.
            "last_error": last_error if status == "auth_required" else None,
            "peer_flood_ts": peer_flood_ts,  # Ф69-1 E: проблема сторожа `account:<id>:peer_flood`
        }
        for aid, source, title, status, last_ok_ts, flood_until, has_secret, last_error, peer_flood_ts in (
            account_rows
        )
    ]

    dead_letter_total = 0
    outbox_unknown = 0
    for schema in ("main", "work"):
        dead_letter_total += conn.execute(
            _sql(schema, "SELECT COUNT(*) FROM {schema}.dead_letter")
        ).fetchone()[0]
        outbox_unknown += conn.execute(
            _sql(schema, "SELECT COUNT(*) FROM {schema}.outbox WHERE status = 'unknown'")
        ).fetchone()[0]

    last_backup_ts = conn.execute(
        "SELECT MAX(ts) FROM main.audit WHERE action = 'backup' AND result = 'ok'"
    ).fetchone()[0]

    return {
        "now": now_ms,
        "accounts": accounts,
        "dead_letter_total": dead_letter_total,
        "outbox_unknown": outbox_unknown,
        "last_backup_ts": last_backup_ts,
        # Поправки после opus-критика кода (R4): отметка времени источников — hub-watch поднимает
        # `retention_stalled`, если источники молчат дольше недели (ретенция work-БД тогда законно стоит,
        # раздел F.3, п.3/R3 — это не баг, а её защита от кривых настенных часов).
        "source_time_hwm": retention.get_source_time_hwm(conn),
    }
