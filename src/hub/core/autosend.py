"""Отправка без подтверждения владельца в выбранные им беседы (Ф37-1, docs/architecture.md).

Список «без подтверждения» — пары (беседа, роль) в `main.channel_autosend`; ведёт только владелец (UI-сессия,
аварийно — CLI по внутреннему сокету). Черновик bearer-сессии подтверждается сам, только если выполнено ВСЁ
из B2 (`try_send`), — в той же транзакции, что `outbox.enqueue`. Всё остальное — обычный черновик.

П2: роль сессия назначает себе сама, токен профиля общий — разрешение «беседа + роль» действует для ЛЮБОЙ
сессии проекта беседы, подписавшейся этой ролью. Это свойство записано в спеке; поэтому у каждой авто-отправки
аудит `outbox_autosend` с номером подписки и разговором, а у сообщения — `outbox.auto_role`.

Все функции записи — только внутри `Db.write()`, время — параметром `now_ms`.
"""

from typing import NamedTuple

from . import autopilot as autopilot_mod
from . import bot_guard, draft_channels
from . import card_route as card_route_mod
from . import held as held_mod
from . import outbox as outbox_mod
from . import subs as subs_mod
from .db import db_of, next_seq
from .security import check_attach

# B2.6 + П8: авто-отправок в ОДНУ беседу за час (по беседе целиком — разные написания роли и вторая сессия
# роли лимит не множат) и общий предохранитель на токен по всем беседам.
AUTOSEND_PER_HOUR = 40  # Ф66-1: слово владельца — было 20
AUTOSEND_TOKEN_PER_HOUR = 60
_HOUR_MS = 60 * 60 * 1000
# Ф41-5б (5.4, П8): предохранители ветки выключателя автопилота — по сессии хаба, не по токену; первые
# сообщения новым адресатам — ещё и не больше `auto_new_daily` в сутки на аккаунт.
AUTOPILOT_SESSION_PER_HOUR = 60
AUTOPILOT_SESSION_PER_DAY = 300
AUTO_NEW_DAILY_DEFAULT = 20
AUTO_NEW_DAILY_MAX = 200
# Ф63-2 (П9): первых сообщений ботам веткой `bot_chat` в сутки на аккаунт — свой счёт, `auto_new_daily` не
# тратит.
BOT_NEW_DAILY_DEFAULT = 20
BOT_NEW_DAILY_MAX = 200
BOT_CHAT = "bot_chat"
_DAY_MS = 24 * _HOUR_MS


class AutosendError(Exception):
    """Отказ правки списка: `status` — HTTP-код (404 — беседы нет, 422 — беседе нельзя или роль кривая)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class Draft(NamedTuple):
    """Черновик для `try_send`, когда строку создал не `outbox.enqueue` (заготовка новому адресату)."""

    outbox_id: int
    created: bool
    status: str
    paced: bool


class Auto(NamedTuple):
    """Итог `try_send`: `role` — роль подписки, чей черновик ушёл сразу; `denied` — причина, по которой он
    остался черновиком и о которой сессии говорится (B4: только лимит — `limit`, какой именно — `limit_of`:
    `channel` или `token`). Любое другое невыполненное условие — `None` вместо `Auto` (без пометок)."""

    role: str | None = None
    denied: str | None = None
    limit_of: str | None = None
    # Ф41-5б: `autopilot` — ушло веткой выключателя (`hub_send` / `hub_send_new`); у ветки списка — `None`.
    by: str | None = None
    mode: str | None = None  # режим подтверждения: `None` — сразу, `paced`, `send_at`
    # Ф63-2 (1.3, П9): `denied == "guard"` — путь «сразу» в беседе с ботом остановила страховка; категория
    # (`bot_guard.CATEGORIES`): черновик ждёт владельца с этой пометкой.
    guard: str | None = None


# --- список (A1–A3) ----------------------------------------------------------------------------------------


def roles(conn, channel_id: int) -> list[str]:
    """A2: роли, которым беседа разрешена без подтверждения (как написал владелец), по порядку включения."""
    rows = conn.execute(
        "SELECT role FROM main.channel_autosend WHERE channel_id = ? ORDER BY created_ts, role_key",
        (channel_id,),
    ).fetchall()
    return [row[0] for row in rows]


def list_all(conn) -> list[dict]:
    """A3 (`hub channel autosend list`): весь список — `{channel_id, role, created_ts}`."""
    rows = conn.execute(
        "SELECT channel_id, role, created_ts FROM main.channel_autosend "
        "ORDER BY channel_id, created_ts, role_key"
    ).fetchall()
    return [{"channel_id": row[0], "role": row[1], "created_ts": row[2]} for row in rows]


def _role_form(value: str) -> tuple[str, str]:
    """A1: роль списка нормализуется тем же `subs.role_form`, что роли подписок; пустая — 422."""
    try:
        role, role_key = subs_mod.role_form(value)
    except subs_mod.SubError as exc:
        raise AutosendError(422, str(exc.detail)) from exc
    if role is None or role_key is None:
        raise AutosendError(422, "role: пусто — назови роль подписки")
    return role, role_key


def set_role(conn, channel_id: int, *, role: str, on: bool, now_ms: int) -> bool:
    """A2: включить/выключить пару (беседа, роль). Включить можно, если беседа существует, лежит в проекте (не
    «Неразобранное»), не в архиве, не заготовка `new:…` и не `no_reply` — иначе 422; выключить — всегда (A5:
    перенос и архив список не чистят, убрать строку владелец может в любом состоянии беседы). Изменение двигает
    `channel.seq` — UI получает поле `autosend` через `/sync`. Возвращает, изменилось ли что-то."""
    shown, role_key = _role_form(role)
    schema = db_of(channel_id)
    row = conn.execute(
        f"SELECT project_id, archived, ext_id, meta_json FROM {schema}.channel WHERE id = ?",  # noqa: S608
        (channel_id,),
    ).fetchone()
    if row is None:
        raise AutosendError(404, "not found")
    project_id, archived, ext_id, meta_json = row
    existing = conn.execute(
        "SELECT 1 FROM main.channel_autosend WHERE channel_id = ? AND role_key = ?", (channel_id, role_key)
    ).fetchone()
    if on:
        if project_id is None:
            raise AutosendError(422, "беседа в «Неразобранном» — сначала разложи её в проект")
        if archived:
            raise AutosendError(422, "беседа в архиве")
        if draft_channels.is_temp_ext_id(ext_id):
            raise AutosendError(422, "новая беседа — первое сообщение подтверждает владелец")
        if outbox_mod.channel_no_reply(meta_json):
            raise AutosendError(422, outbox_mod.NO_REPLY_ERROR)
        if existing is not None:
            return False
        conn.execute(
            "INSERT INTO main.channel_autosend (channel_id, role_key, role, created_ts) VALUES (?, ?, ?, ?)",
            (channel_id, role_key, shown, now_ms),
        )
    else:
        if existing is None:
            return False
        conn.execute(
            "DELETE FROM main.channel_autosend WHERE channel_id = ? AND role_key = ?", (channel_id, role_key)
        )
    conn.execute(
        f"UPDATE {schema}.channel SET seq = ? WHERE id = ?",  # noqa: S608
        (next_seq(conn), channel_id),
    )
    return True


# --- отправка (B2 + П3, П8) --------------------------------------------------------------------------------


class NoRoleSub(Exception):
    """`role_sub`: у запроса нет «подписанной сессии проекта с ролью». `reason` — что именно не так (слова
    для отказа ручки; авто-отправка причину не показывает — черновик просто остаётся черновиком)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def role_sub(
    conn, *, principal, token_name: str | None, session, project_id: int | None, now_ms: int, waiting=None
):
    """Общая проверка Ф37-1 B2.1–B2.3 и Ф37-3 B1: запрос пришёл от ПОДПИСАННОЙ СЕССИИ ПРОЕКТА С РОЛЬЮ.
    Возвращает её подписку (`subs.SubRec`) или бросает `NoRoleSub`:

    - принципал — bearer; токен интерактивный со скоупом `draft` и проходит `security.check_attach` (не
      дежурная, не оркестратор, не фоновый);
    - в запросе есть `session` (pid + `starttime` + разговор), процесс жив;
    - у ЭТОЙ сессии живая подписка на ПРОЕКТ `project_id` с ролью. Подписка на беседу, подписка без роли,
      подписка другого проекта и агент вне проектов не годятся; брошенная подписка OpenCode — тоже (П3:
      `subs._sub_dead`, процесс сервера жив всегда)."""
    if principal.is_ui or not principal.has_scope("draft"):
        raise NoRoleSub("нужен bearer-токен сессии со скоупом draft")
    try:
        check_attach(name=token_name, kind=principal.kind, scopes=principal.scopes)
    except ValueError as exc:
        raise NoRoleSub("только из интерактивной сессии (не дежурная и не оркестратор)") from exc
    if session is None:
        raise NoRoleSub("сессия не определена (session)")
    if not subs_mod.is_alive(session.pid, session.proc_start, getattr(session, "host", None)):
        raise NoRoleSub("сессия не найдена")
    if project_id is None or project_id == subs_mod.NO_PROJECT_ID:
        raise NoRoleSub("беседа не в проекте")
    row = conn.execute(
        "SELECT id FROM main.session_sub WHERE api_token_id = ? AND session_pid = ? "
        "AND session_proc_start = ? AND session_id = ? AND project_id = ? AND ended_ts IS NULL "
        "AND role_key IS NOT NULL ORDER BY id DESC LIMIT 1",
        (principal.token_id, session.pid, session.proc_start, session.session_id, project_id),
    ).fetchone()
    rec = subs_mod.get_sub(conn, row[0]) if row is not None else None
    if rec is None or not rec.role_key or subs_mod._sub_dead(rec, now_ms, waiting):
        raise NoRoleSub('нужна живая подписка сессии на проект беседы с ролью — hub_subscribe(role="…")')
    return rec


def list_allows(conn, *, channel_id: int, role_key: str, session) -> bool:
    """Ветка списка владельца (Ф37-1): пара (беседа, роль) есть в `main.channel_autosend` и список не закрыт
    для этой сессии. Ф41-5 (3.3): сессии хаба с `autosend_ok = 0` (напарник, заведённый по заявке) она
    закрыта."""
    allowed = conn.execute(
        "SELECT 1 FROM main.channel_autosend WHERE channel_id = ? AND role_key = ?",
        (channel_id, role_key),
    ).fetchone()
    return allowed is not None and not _list_closed(conn, session)


def list_sub(
    conn,
    *,
    principal,
    token_name: str | None,
    session,
    channel_id: int,
    project_id: int | None,
    now_ms: int,
    waiting=None,
):
    """Общая проверка «беседа из списка „без подтверждения“ для роли сессии» — её зовут и черновик
    (`try_send`), и нажатие кнопки бота (Ф44-1c, `presses.create`): `(подписка, разрешает ли список)`.
    `NoRoleSub` — запрос не от подписанной сессии проекта беседы с ролью (`role_sub`). Условия менять
    только по прямому слову владельца: это граница правила «хаб не отправляет сам»."""
    sub = role_sub(
        conn,
        principal=principal,
        token_name=token_name,
        session=session,
        project_id=project_id,
        now_ms=now_ms,
        waiting=waiting,
    )
    return sub, list_allows(conn, channel_id=channel_id, role_key=sub.role_key, session=session)


def _sent_count(conn, now_ms: int, *, channel_id: int | None = None, token_id: int | None = None) -> int:
    """П8: авто-отправок за последний час — по беседе целиком или по токену во всех беседах (оба файла).
    Ф66-1, ревью Р5: вместе с «сразу да» таблицы маршрута (`card_route.route_count`) — пределы общие."""
    since = now_ms - _HOUR_MS
    if channel_id is not None:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {db_of(channel_id)}.outbox "  # noqa: S608
            "WHERE channel_id = ? AND auto_role IS NOT NULL AND created_ts > ?",
            (channel_id, since),
        ).fetchone()
        return row[0] + card_route_mod.route_count(conn, "draft", since, channel_id=channel_id)
    total = card_route_mod.route_count(conn, "draft", since, token_id=token_id) if token_id is not None else 0
    for schema in ("main", "work"):
        row = conn.execute(
            f"SELECT COUNT(*) FROM {schema}.outbox "  # noqa: S608
            "WHERE api_token_id = ? AND auto_role IS NOT NULL AND created_ts > ?",
            (token_id, since),
        ).fetchone()
        total += row[0]
    return total


def try_send(
    conn,
    *,
    principal,
    token_name: str | None,
    session,
    channel_id: int,
    project_id: int | None,
    result: outbox_mod.EnqueueResult,
    send_at_ts: int | None,
    now_ms: int,
    waiting=None,
    session_key: str | None = None,
) -> Auto | None:
    """B2: черновик, только что созданный `outbox.enqueue` в ЭТОЙ транзакции, подтверждается сам, только если
    выполнено всё перечисленное; иначе остаётся обычным черновиком (`None`), а при исчерпанном лимите —
    черновиком с пометкой (`Auto(denied="limit")`, B4).

    1. принципал — bearer, строка создана этим запросом (не повтор по `idempotency_key`), `pending_approval`;
    2. в запросе есть `session`; токен интерактивный со скоупом `draft` и проходит `security.check_attach`
       (не дежурная, не оркестратор, не рабочая сессия); процесс жив;
    3. у сессии живая подписка на проект беседы с ролью (пункты 1–3 — `role_sub`, П3);
    4. пара (беседа, `role_key`) есть в списке владельца;
    5. черновик обычный: не по графику (`result.paced` — фактический, П8), без `send_at`, беседа не
       заготовка `new:…`. Файлы уходят сразу, как и текст (владелец 04.10.2026: «должны полно отвечать на
       разрешённые чаты» — снимает П6): что сессия вообще может приложить, ограничивает MCP (только из
       рабочей папки проекта, не служебные файлы) и скоуп `attach` токена;
    6. лимиты П8.

    Подтверждение — `outbox.approve_mode` (те же проверки, что у кнопки владельца: `no_reply`, пустой текст) с
    действием журнала `outbox_autosend` (B3) и отметкой `outbox.auto_role`. Отметку прочтения беседы
    авто-отправка НЕ двигает (П9)."""
    if principal.is_ui or not result.created or result.status != "pending_approval":
        return None
    try:
        sub, by_list = list_sub(
            conn,
            principal=principal,
            token_name=token_name,
            session=session,
            channel_id=channel_id,
            project_id=project_id,
            now_ms=now_ms,
            waiting=waiting,
        )
    except NoRoleSub:
        return None
    schema = db_of(channel_id)
    channel_row = conn.execute(
        f"SELECT c.ext_id, c.account_id, c.meta_json, c.kind, a.source FROM {schema}.channel c "  # noqa: S608
        "JOIN main.account a ON a.id = c.account_id WHERE c.id = ?",
        (channel_id,),
    ).fetchone()
    if channel_row is None:
        return None
    temp = draft_channels.is_temp_ext_id(channel_row[0])
    if temp and draft_channels.is_msg_author_draft(channel_row[2]):
        # Ф46-1 П1: первое сообщение автору из группы — только кнопкой владельца, и при снятом выключателе
        # `hub_send_new` (риск `PeerFlood` на весь личный аккаунт). Текстом и с файлами — оба пути здесь.
        return None
    # Ф63-2 (1.2, П2): беседа с Telegram-ботом — настоящая (`attrs.tg_type == "bot"`) или заготовка по нику
    # на «bot». Любой путь «сразу» в ней проходит страховку (`bot_guard`, 1.3).
    bot = bot_guard.chat_is_bot(channel_row[4], channel_row[3], channel_row[0], channel_row[2])
    if not by_list or result.paced or send_at_ts is not None or temp:
        if bot:
            # Ф63-2 (1.2): порядок — список → `bot_chat` → прежние `hub_send` / `hub_send_new`.
            auto = _try_bot_chat(
                conn,
                session=session,
                session_key=session_key,
                sub=sub,
                channel_id=channel_id,
                account_id=channel_row[1],
                project_id=project_id,
                temp=temp,
                outbox_id=result.outbox_id,
                now_ms=now_ms,
            )
            if auto is not None:
                return auto
        # Ф41-5б (5.4): вторая ветка — выключатель автопилота.
        return _try_autopilot(
            conn,
            session=session,
            session_key=session_key,
            sub=sub,
            channel_id=channel_id,
            account_id=channel_row[1],
            project_id=project_id,
            temp=temp,
            outbox_id=result.outbox_id,
            now_ms=now_ms,
            bot=bot,
        )
    if bot:
        # Ф63-2 (1.3, П6): и из списка «без подтверждения» в беседе с ботом — оплата, коды, удаление,
        # личные данные и файлы идут карточкой (ужесточение Ф37-1).
        hit = _guard(conn, outbox_id=result.outbox_id, channel_id=channel_id, path="list", now_ms=now_ms)
        if hit is not None:
            return Auto(denied="guard", guard=hit)
    if _sent_count(conn, now_ms, channel_id=channel_id) >= AUTOSEND_PER_HOUR:
        return Auto(denied="limit", limit_of="channel")
    if _sent_count(conn, now_ms, token_id=principal.token_id) >= AUTOSEND_TOKEN_PER_HOUR:
        return Auto(denied="limit", limit_of="token")
    outbox_mod.approve_mode(
        conn,
        result.outbox_id,
        now_ms=now_ms,
        audit_action="outbox_autosend",
        audit_detail={"sub_id": sub.id, "role": sub.role, "session_id": session.session_id},
    )
    conn.execute(
        f"UPDATE {schema}.outbox SET auto_role = ? WHERE id = ?",  # noqa: S608
        (sub.role, result.outbox_id),
    )
    return Auto(role=sub.role)


# --- Ф41-5б (5.4, П7, П8): ветка выключателя автопилота ----------------------------------------------------


def _list_closed(conn, session) -> bool:
    """Ф41-5 (3.3): запрос от сессии хаба, которой список «без подтверждения» закрыт (`autosend_ok = 0`).
    Тройка здесь только ЗАКРЫВАЕТ ветку — прав не даёт, поэтому ключ запуска не нужен."""
    hub_id = held_mod.held_id(getattr(session, "pid", None))
    if hub_id is None:
        return False
    rec = held_mod.get_open(conn, hub_id)
    return rec is not None and not rec.autosend_ok


def auto_new_daily(config_json: str | None) -> int:
    """П8: настройка аккаунта `auto_new_daily` (`hub account set N auto_new_daily=…`): целое 0…200, больше —
    200, кривое или нет ключа — 20."""
    import json

    try:
        config = json.loads(config_json) if config_json else {}
    except ValueError, TypeError:
        config = {}
    value = config.get("auto_new_daily") if isinstance(config, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return AUTO_NEW_DAILY_DEFAULT
    return min(value, AUTO_NEW_DAILY_MAX)


def _session_count(conn, hub_session_id: int, since: int) -> int:
    """Авто-отправки сессии хаба веткой выключателя после `since` — вместе с «сразу да» её черновиков
    (Ф66-1, ревью Р5)."""
    total = card_route_mod.route_count(conn, "draft", since, session_id=hub_session_id)
    for schema in ("main", "work"):
        total += conn.execute(
            f"SELECT COUNT(*) FROM {schema}.outbox WHERE auto_session = ? AND created_ts > ?",  # noqa: S608
            (hub_session_id, since),
        ).fetchone()[0]
    return total


def _new_count(conn, account_id: int, since: int) -> int:
    """Первых сообщений, ушедших веткой выключателя с этого аккаунта, — по журналу (`new_account` в записи
    `outbox_autosend`)."""
    return conn.execute(
        "SELECT COUNT(*) FROM main.audit WHERE action = 'outbox_autosend' AND ts > ? "
        "AND json_extract(detail_json, '$.new_account') = ?",
        (since, account_id),
    ).fetchone()[0]


def _try_autopilot(
    conn,
    *,
    session,
    session_key: str | None,
    sub,
    channel_id: int,
    account_id: int,
    project_id: int | None,
    temp: bool,
    outbox_id: int,
    now_ms: int,
    bot: bool = False,
) -> Auto | None:
    """5.4: сессия хаба в авто при СНЯТОМ выключателе `hub_send` (ответ в беседу своего проекта) или
    `hub_send_new` (первое сообщение в беседу-заготовку) — единственная проверка `autopilot.session_free`
    (ключ запуска, режим, свой проект, выключатель). Подтверждение — только `outbox.approve_mode` (режим тот
    же, что у клика: сразу / по графику / в срок) со своим аудитом `outbox_autosend` `{"by": "autopilot"}` и
    отметками `auto_role`, `auto_session`. Побочных действий клика владельца НЕТ (П7): беседа не читается,
    карточка разборщика не закрывается, исход «подтверждено владельцем» автору не пишется."""
    key = "hub_send_new" if temp else "hub_send"
    free = autopilot_mod.session_free(conn, autopilot_mod.ident(session, session_key), key, project_id)
    if free is None:
        return None
    if bot:
        # Ф63-2 (1.3): беседа с ботом и этим путём — через страховку.
        hit = _guard(conn, outbox_id=outbox_id, channel_id=channel_id, path="autopilot", now_ms=now_ms)
        if hit is not None:
            return Auto(denied="guard", guard=hit, by="autopilot")
    if _sent_count(conn, now_ms, channel_id=channel_id) >= AUTOSEND_PER_HOUR:
        return Auto(denied="limit", limit_of="channel", by="autopilot")
    if _session_count(conn, free.id, now_ms - _HOUR_MS) >= AUTOPILOT_SESSION_PER_HOUR:
        return Auto(denied="limit", limit_of="session", by="autopilot")
    if _session_count(conn, free.id, now_ms - _DAY_MS) >= AUTOPILOT_SESSION_PER_DAY:
        return Auto(denied="limit", limit_of="session_day", by="autopilot")
    detail = {"by": "autopilot", "hub_session_id": free.id, "sub_id": sub.id, "role": sub.role}
    if temp:
        config = conn.execute("SELECT config_json FROM main.account WHERE id = ?", (account_id,)).fetchone()
        if _new_count(conn, account_id, now_ms - _DAY_MS) >= auto_new_daily(config[0] if config else None):
            return Auto(denied="limit", limit_of="new_daily", by="autopilot")
        detail["new_account"] = account_id
    approved = outbox_mod.approve_mode(
        conn, outbox_id, now_ms=now_ms, audit_action="outbox_autosend", audit_detail=detail
    )
    conn.execute(
        f"UPDATE {db_of(channel_id)}.outbox SET auto_role = ?, auto_session = ? WHERE id = ?",  # noqa: S608
        (sub.role, free.id, outbox_id),
    )
    return Auto(role=sub.role, by="autopilot", mode=approved.mode)


# --- Ф63-2 (1.2, 1.3, П1, П6, П9): беседы с Telegram-ботами -----------------------------------------------


def bot_new_daily(config_json: str | None) -> int:
    """П9: настройка аккаунта `bot_new_daily` (`hub account set N bot_new_daily=…`): целое 0…200, больше —
    200, кривое или нет ключа — 20."""
    import json

    try:
        config = json.loads(config_json) if config_json else {}
    except ValueError, TypeError:
        config = {}
    value = config.get("bot_new_daily") if isinstance(config, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return BOT_NEW_DAILY_DEFAULT
    return min(value, BOT_NEW_DAILY_MAX)


def _bot_new_count(conn, account_id: int, since: int) -> int:
    """Первых сообщений ботам веткой `bot_chat` с аккаунта — по журналу (`bot_new` в `outbox_autosend`)."""
    return conn.execute(
        "SELECT COUNT(*) FROM main.audit WHERE action = 'outbox_autosend' AND ts > ? "
        "AND json_extract(detail_json, '$.bot_new') = ?",
        (since, account_id),
    ).fetchone()[0]


def _guard(conn, *, outbox_id: int, channel_id: int, path: str, now_ms: int) -> str | None:
    """Страховка черновика в беседе с ботом: категория (и запись `bot_guard` без текста) или `None`."""
    hit = bot_guard.outbox_category(conn, outbox_id, now_ms=now_ms)
    if hit is not None:
        bot_guard.audit_hit(
            conn, category=hit, channel_id=channel_id, path=path, now_ms=now_ms, outbox_id=outbox_id
        )
    return hit


def _try_bot_chat(
    conn,
    *,
    session,
    session_key: str | None,
    sub,
    channel_id: int,
    account_id: int,
    project_id: int | None,
    temp: bool,
    outbox_id: int,
    now_ms: int,
) -> Auto | None:
    """1.2: сессия хаба в авто при СНЯТОМ выключателе `bot_chat`, беседа с ботом своего проекта (проверка —
    та же `autopilot.session_free`: ключ запуска, режим, проект сессии = проект беседы). `None` — выключатель
    не про эту сессию (дальше — прежние `hub_send` / `hub_send_new`); иначе итог: ушло, страховка или предел.

    П1: без графика — признак `paced` первого сообщения (заготовки) снимается в этой транзакции до
    `approve_mode` (не ждёт окна, не занимает `pace_daily` аккаунта); явный `paced=True` сессии в существующей
    беседе остаётся (раздел 8 спеки), такая строка и строка со `send_at` перепроверяются при выдаче
    (`outbox.lease`). П6: файлы — карточкой (страховка `file`). Пределы — как у `hub_send`
    (на беседу и на сессию); первое сообщение боту — свой счёт `bot_new_daily` (журнал `bot_new`), не
    `auto_new_daily`. Аудит `outbox_autosend` `{"by": "bot_chat"}`, отметки `auto_role` / `auto_session`.
    Побочных действий клика владельца нет — как у `_try_autopilot` (П7 Ф41-5б)."""
    free = autopilot_mod.session_free(conn, autopilot_mod.ident(session, session_key), BOT_CHAT, project_id)
    if free is None:
        return None
    hit = _guard(conn, outbox_id=outbox_id, channel_id=channel_id, path=BOT_CHAT, now_ms=now_ms)
    if hit is not None:
        return Auto(denied="guard", guard=hit, by=BOT_CHAT)
    if _sent_count(conn, now_ms, channel_id=channel_id) >= AUTOSEND_PER_HOUR:
        return Auto(denied="limit", limit_of="channel", by=BOT_CHAT)
    if _session_count(conn, free.id, now_ms - _HOUR_MS) >= AUTOPILOT_SESSION_PER_HOUR:
        return Auto(denied="limit", limit_of="session", by=BOT_CHAT)
    if _session_count(conn, free.id, now_ms - _DAY_MS) >= AUTOPILOT_SESSION_PER_DAY:
        return Auto(denied="limit", limit_of="session_day", by=BOT_CHAT)
    detail = {"by": BOT_CHAT, "hub_session_id": free.id, "sub_id": sub.id, "role": sub.role}
    if temp:
        config = conn.execute("SELECT config_json FROM main.account WHERE id = ?", (account_id,)).fetchone()
        if _bot_new_count(conn, account_id, now_ms - _DAY_MS) >= bot_new_daily(config[0] if config else None):
            return Auto(denied="limit", limit_of="bot_new_daily", by=BOT_CHAT)
        detail["bot_new"] = account_id
    schema = db_of(channel_id)
    if temp:
        # Раздел 8 спеки: снимается только неявный график первого сообщения (заготовке он ставится всегда);
        # явный `paced=True` сессии в существующей беседе остаётся.
        conn.execute(f"UPDATE {schema}.outbox SET paced = 0 WHERE id = ?", (outbox_id,))  # noqa: S608
    approved = outbox_mod.approve_mode(
        conn, outbox_id, now_ms=now_ms, audit_action="outbox_autosend", audit_detail=detail
    )
    conn.execute(
        f"UPDATE {schema}.outbox SET auto_role = ?, auto_session = ? WHERE id = ?",  # noqa: S608
        (sub.role, free.id, outbox_id),
    )
    return Auto(role=sub.role, by=BOT_CHAT, mode=approved.mode)
