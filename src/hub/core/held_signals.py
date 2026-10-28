"""Сигналы постоянной сессии хаба: что готово и выдача хосту. Ф41-4, docs/architecture.md–2.4 и поправки раздела 10 (П2,
П5, П6, П11–П14 — они приоритетнее).

Очередь сигналов и сама выдача — прежние (`subs.deliver`, `chan_subs.deliver` и родственные): второй очереди
нет. Меняется только, кто забирает: внешняя сессия — командой `hub sub wait` своим токеном, сессия хаба — хост
двумя ручками: раздел `sessions.signals` своего длинного ожидания («что готово») и
`POST /work/host/sessions/{id}/signals` («выдать»).

Видимые проекты для годности — как у токена-создателя подписки (хост их не знает и не задаёт); аудит
`sub_signal` пишется с этим токеном. Отозванный токен подписку завершает (`subs.cleanup`) — здесь он просто
ничего не видит.

Текстов нет: сигнал — только числа и метки из перечня. Запись — только внутри `Db.write()`."""

import json
import uuid

from . import chan_subs as chan_subs_mod
from . import draft_outcomes as draft_outcomes_mod
from . import group_ops as group_ops_mod
from . import held as held_mod
from . import held_work, hub_sessions
from . import human_requests as human_requests_mod
from . import peer_msgs as peer_msgs_mod
from . import presses as presses_mod
from . import reminders as reminders_mod
from . import subs as subs_mod
from .hub_sessions import SessionError

CLIENT_NAME = "hub-work"  # в аудите выдачи: забрал хост постоянных сессий


def _token_scope(conn, token_id: int) -> tuple[bool, frozenset[int] | None]:
    """`(жив ли токен, его проекты)`; проекты `None` — все. Отозванный или пропавший токен — `(False, …)`."""
    row = conn.execute(
        "SELECT revoked, project_scope_json FROM main.api_token WHERE id = ?", (token_id,)
    ).fetchone()
    if row is None or row[0]:
        return False, frozenset()
    if row[1] is None:
        return True, None
    try:
        return True, frozenset(json.loads(row[1]))
    except ValueError, TypeError:
        return True, frozenset()


def project_sub(conn, rec: held_mod.HubSession):
    """Живая подписка сессии на проект (`subs.SubRec`) или `None`."""
    return hub_sessions._live_sub(conn, rec.id) if rec.closed_ts is None else None  # noqa: SLF001


def chan_subs_of(conn, rec: held_mod.HubSession) -> dict[int, list]:
    """Живые подписки сессии на беседы, по токену-создателю (у сессии с двумя серверами хаба их два)."""
    pid, proc_start, session_id = held_mod.triple(rec.id)
    by_token: dict[int, list] = {}
    for chan in chan_subs_mod.live_of_process(conn, pid, proc_start):
        if chan.session_id == session_id:
            by_token.setdefault(chan.api_token_id, []).append(chan)
    return by_token


class Waiting:
    """Что ждёт сессию: `ready` — годное к выдаче СЕЙЧАС (дебаунс прошёл, срок пачки и расшифровка — тоже),
    `later` — годное, но ждущее срока, `next_ms` — через сколько мс станет готово ближайшее (`None` — нечему).
    Ф50-1: `urgent` — сколько из готового срочно (сообщение агента с `urgent`, сообщение владельца агенту, карточка
    делегату); входит в `ready`. 21.10: и личное — личная карточка (`subs.personal_card`) и сигнал сообщений
    личной категории (`chan_subs.PERSONAL_CATS`)."""

    __slots__ = ("later", "next_ms", "ready", "urgent")

    def __init__(self) -> None:
        self.ready = 0
        self.urgent = 0
        self.later = 0
        self.next_ms: int | None = None

    def hold(self, count: int, left_ms: int) -> None:
        self.later += count
        if left_ms > 0 and (self.next_ms is None or left_ms < self.next_ms):
            self.next_ms = left_ms

    @property
    def total(self) -> int:
        return self.ready + self.later


def waiting(conn, rec: held_mod.HubSession, *, now_ms: int, presence: list[dict]) -> Waiting:
    """П2, читатель: ждущие сигналы сессии всех видов — тем же чтением, что годность при выдаче. Карточка
    прочитанная, отвеченная, закрытая или перенесённая не считается; беседа перед глазами владельца — тоже."""
    result = Waiting()
    sub = project_sub(conn, rec)
    if sub is not None:
        alive, visible = _token_scope(conn, sub.api_token_id)
        if alive:
            # 21.10: `personal` — личное среди готового (личные карточки и сигналы сообщений `dm`/`mention`/
            # `thread`): срочно, как сообщение владельца.
            if sub.mode == "messages":
                count, later, held_ms, personal = chan_subs_mod.valid_state(
                    conn, now_ms=now_ms, project_sub=sub, visible_projects=visible
                )
            else:
                # Ф67-1: карточки подписки с пачкой ждут её срока.
                count, later, held_ms, personal = subs_mod.cards_state(
                    conn, sub, visible_projects=visible, presence=presence, now_ms=now_ms
                )
            count += reminders_mod.signal_count(conn, sub)
            count += peer_msgs_mod.signal_count(conn, sub, now_ms=now_ms)
            count += draft_outcomes_mod.signal_count(conn, sub, now_ms=now_ms)
            count += presses_mod.signal_count(conn, sub, now_ms=now_ms)
            # Ф54-1 (П11): исходы заявок «нужен человек» — владелец прошёл препятствие и ждёт у открытой вкладки.
            humans = human_requests_mod.signal_count(conn, sub, now_ms=now_ms)
            count += humans
            # Ф62-6: исходы операций над группами WhatsApp — сессия ждёт ссылку или беседу группы.
            grouped = group_ops_mod.signal_count(conn, sub, now_ms=now_ms)
            count += grouped
            # Ф50-1: срочное — сообщения мимо пачки (владелец, `urgent`); Ф54-1: и исходы заявок «нужен человек»
            # (будят и «тяжёлую холодную»). Ф66-1: карточки делегату (Ф49) убраны. 21.10: и личное.
            urgent = personal + humans + grouped + peer_msgs_mod.urgent_count(conn, sub, now_ms=now_ms)
            # Ф48-4 п.3: сообщения агентов, ждущие срока пачки, — не готовое (спящую не поднимают, свободную
            # не будят); срок пришёл — войдут в `signal_count` выше.
            peer_later, peer_ms = peer_msgs_mod.batch_state(conn, sub, now_ms=now_ms)
            left_ms = subs_mod.debounce_left_ms(sub, now_ms) if count else 0
            if count and left_ms == 0:
                # Выдача `messages` отдаёт и несозревшие «по ключу» тем же выходом; в счёт готового они
                # не входят.
                result.ready += count
                result.urgent += urgent
                result.hold(later, held_ms)
            else:
                result.hold(count, left_ms)
                result.hold(later, held_ms)
                # При готовом пачку не держим: выдача готового заберёт и накопленное.
                result.hold(peer_later, peer_ms)
    for token_id, chans in chan_subs_of(conn, rec).items():
        alive, _visible = _token_scope(conn, token_id)
        if not alive:
            continue
        count, later, held_ms, personal = chan_subs_mod.valid_state(
            conn, now_ms=now_ms, chan_sub_ids=[c.id for c in chans]
        )
        left_ms = chan_subs_mod.debounce_left_ms(chans, now_ms) if count else 0
        if count and left_ms == 0:
            result.ready += count
            result.urgent += personal  # 21.10: личное в беседе под подпиской — срочно
        else:
            result.hold(count, left_ms)
        result.hold(later, held_ms)
    return result


def snapshot(conn, *, now_ms: int, presence: list[dict]) -> list[dict]:
    """П2: раздел `sessions.signals` ожидания хоста — `[{"id", "ready", "urgent", "next_ms"}]` по всем
    незакрытым сессиям хаба, у которых есть ждущее (готовое или со сроком). Ф50-1: `urgent` — сколько из
    `ready` срочно (такое хост подаёт и посреди хода, и спящей — первой в очереди темпа)."""
    items: list[dict] = []
    for rec in held_mod.list_sessions(conn):
        state = waiting(conn, rec, now_ms=now_ms, presence=presence)
        if state.ready or state.next_ms is not None:
            items.append(
                {"id": rec.id, "ready": state.ready, "urgent": state.urgent, "next_ms": state.next_ms}
            )
    return items


def chan_actions(conn) -> list[tuple[int, list]]:
    """П12, читатель: что сделать с подписками сессий хаба на беседы (архив и перенос беседы — конец
    подписки, разложили из «Неразобранного» — `adopt`) — то же, что цикл `/chan-subs/wait` делает перед
    выдачей. `[(token_id, действия)]`; пусто — делать нечего."""
    found: list[tuple[int, list]] = []
    for rec in held_mod.list_sessions(conn):
        for token_id, chans in chan_subs_of(conn, rec).items():
            alive, visible = _token_scope(conn, token_id)
            if not alive:
                continue
            actions = chan_subs_mod.lazy_actions(conn, chans, visible)
            if actions:
                found.append((token_id, actions))
    return found


def apply_chan_actions(conn, found: list[tuple[int, list]], *, now_ms: int) -> None:
    request_id = uuid.uuid4().hex
    for token_id, actions in found:
        chan_subs_mod.apply_actions(
            conn,
            actions,
            now_ms=now_ms,
            actor="core",
            api_token_id=token_id,
            client_name=CLIENT_NAME,
            request_id=request_id,
        )


def pending_total(conn, rec: held_mod.HubSession, *, now_ms: int, presence: list[dict]) -> int:
    """`sub.pending` записи сессии: ждущие сигналы всех видов (годные; и готовые, и ждущие срока)."""
    return waiting(conn, rec, now_ms=now_ms, presence=presence).total


def deliver(
    conn, hub_session_id: int, *, now_ms: int, presence: list[dict], closing: bool = False, mid: bool = False
) -> dict:
    """2.3 + П6, П12, П14: выдача хосту одной транзакцией писателя. Для живой подписки сессии на проект — то
    же, что `/subs/{id}/wait` отдаёт клиенту протокола 5 в момент выдачи (`subs.deliver` с сообщениями
    агентов и исходами черновиков), для подписок на беседы — то же, что `/chan-subs/wait`
    (`chan_subs.deliver` после ленивых проверок). Дебаунс — прежний: под ним выдачи нет.

    П6: пусто и ничего не отмечено, если сессия не `idle`, стоит просьба «усыпить», в очереди есть реплика
    владельца или ядро останавливается (`closing`) — сигнал не обгоняет реплику и не уходит усыпляемой сессии.

    П5: непустая выдача ставит `signal_turn_ts` — до обычного конца хода; оборванный ход станет
    `signal_lost`. Закрытая сессия — 410, нет записи — 404.

    Ф50-1 (П3, П6): `mid` — подача посреди хода: выдаётся ТОЛЬКО при `running` (остальные запреты прежние),
    `signal_turn_ts` не пишется и состояние сессии не трогается — идущий ход не становится «ходом по
    сигналу»; о непрочитанной строке хост сообщает сам (`turn` с `mid_lost`). Выдаётся всё готовое разом."""
    rec = held_work._open_host(conn, hub_session_id)  # noqa: SLF001
    held_work._seen(conn, rec.id, now_ms)  # noqa: SLF001
    sub = project_sub(conn, rec)
    result: dict = {
        "sub_id": sub.id if sub is not None else None,
        "project_id": rec.project_id,
        "signals": [],
        "chan_signals": [],
    }
    # Ф48-2 (A): прерванная реплика и стоящие за ней сигналы не задерживают — считаются только подаваемые.
    queued = held_work.feedable_says(conn, rec)
    wanted = "running" if mid else "idle"
    if closing or rec.state != wanted or rec.sleep_requested_ts is not None or queued:
        return result
    request_id = uuid.uuid4().hex
    if sub is not None:
        alive, visible = _token_scope(conn, sub.api_token_id)
        # Условие — ровно как в цикле `/subs/{id}/wait`: есть готовое (несозревшие «по ключу» одни выдачу не
        # зовут) и дебаунс прошёл.
        ready, _held = subs_mod.wait_state(
            conn,
            sub.id,
            now_ms=now_ms,
            peers=True,
            drafts=True,
            presses=True,
            cards=True,
            humans=True,
            group_ops=True,
        )
        if alive and ready and subs_mod.debounce_left_ms(sub, now_ms) == 0:
            signals = subs_mod.deliver(
                conn,
                sub.id,
                now_ms=now_ms,
                visible_projects=visible,
                presence=presence,
                api_token_id=sub.api_token_id,
                client_name=CLIENT_NAME,
                request_id=request_id,
                peers=True,
                drafts=True,
                presses=True,
                cards=True,
                humans=True,
                group_ops=True,
            )
            result["signals"] = signals or []
        # П14: хост забирал — `last_wait_ts` подписки (и при пустой выдаче).
        subs_mod.touch_wait(conn, sub.id, now_ms=now_ms)
    pid, proc_start, session_id = held_mod.triple(rec.id)
    for token_id, chans in chan_subs_of(conn, rec).items():
        alive, visible = _token_scope(conn, token_id)
        if not alive:
            continue
        actions = chan_subs_mod.lazy_actions(conn, chans, visible)
        if actions:
            # П12: конец подписки при архиве и переносе беседы, `adopt` — перед выдачей, как в ожидании бесед.
            chan_subs_mod.apply_actions(
                conn,
                actions,
                now_ms=now_ms,
                actor="core",
                api_token_id=token_id,
                client_name=CLIENT_NAME,
                request_id=request_id,
            )
            chans = chan_subs_mod.session_subs(conn, token_id, pid, proc_start, session_id)
        if not chans:
            continue
        ready, _held = chan_subs_mod.ready_state(conn, chan_sub_ids=[c.id for c in chans], now_ms=now_ms)
        if ready and chan_subs_mod.debounce_left_ms(chans, now_ms) == 0:
            result["chan_signals"] += chan_subs_mod.deliver(
                conn,
                token_id=token_id,
                pid=pid,
                proc_start=proc_start,
                session_id=session_id,
                project_ids=visible,
                now_ms=now_ms,
                client_name=CLIENT_NAME,
                request_id=request_id,
            )
        chan_subs_mod.touch_wait(conn, [c.id for c in chans], now_ms=now_ms)
    if result["chan_signals"] and not result["signals"] and sub is not None:
        # Ф48-4 п.2: сессию будит сигнал беседы — накопленные в пачке сообщения агентов уходят тем же ходом
        # (только у подписки с пачкой: без неё выдача проекта — прежняя, со своим дебаунсом).
        alive, _visible = _token_scope(conn, sub.api_token_id)
        later, _ms = peer_msgs_mod.batch_state(conn, sub, now_ms=now_ms)
        if alive and later:
            result["signals"] = peer_msgs_mod.take_signals(
                conn,
                sub,
                now_ms=now_ms,
                limit=subs_mod.MAX_SIGNALS_PER_DELIVERY,
                api_token_id=sub.api_token_id,
                client_name=CLIENT_NAME,
                request_id=request_id,
            )
    if not mid and (result["signals"] or result["chan_signals"]):
        hub_sessions.touch(conn, rec.id, now_ms, signal_turn_ts=now_ms)
    return result


def compact(conn, hub_session_id: int, *, now_ms: int, audit: dict) -> held_mod.HubSession:
    """2.5 (часть 4б): «Сжать» — `compact_requested_ts`; хост сожмёт, когда ход не идёт. У спящей процесса
    нет — 409 (переход в сон просьбу и так снимает); закрытая — 409."""
    from .audit import write_audit

    rec = held_work._open_ui(conn, hub_session_id)  # noqa: SLF001
    if rec.state == "asleep":
        raise SessionError(409, "сессия спит — сжать можно, когда она поднята")
    if rec.compact_requested_ts is None:
        hub_sessions.touch(conn, rec.id, now_ms, compact_requested_ts=now_ms)
    detail = {"hub_session_id": rec.id}
    write_audit(conn, now_ms=now_ms, action="hub_session_compact", result="ok", detail=detail, **audit)
    return held_work._session(conn, rec.id)  # noqa: SLF001


def wake(conn, hub_session_id: int, *, now_ms: int, presence: list[dict], audit: dict) -> held_mod.HubSession:
    """2.5: «Поднять» — просьба владельца поднять сессию под ждущее, хотя она тяжёлая и холодная
    (`wake_requested_ts`). Нет ни ждущих сигналов, ни реплик, ни оборванного хода по сигналу: спящая —
    поднимается под постоянную строку (`signal_lost_ts`), поднятая — 409; закрытая — 409."""
    from .audit import write_audit

    rec = held_work._open_ui(conn, hub_session_id)  # noqa: SLF001
    queued = held_work.feedable_says(conn, rec)  # Ф48-2 (A): прерванные и стоящие за ними — не повод
    total = pending_total(conn, rec, now_ms=now_ms, presence=presence)
    sets: dict = {"wake_requested_ts": now_ms}
    if not total and not queued and rec.signal_lost_ts is None:
        if rec.state != "asleep":
            raise SessionError(409, "поднимать не на что")
        # 10.10 (владелец): «Поднять» у спящей сессии поднимает всегда — хост подаст ей постоянную строку
        # «продолжи начатое и проверь входящее» (тот же признак, что у оборванного хода).
        sets["signal_lost_ts"] = now_ms
    hub_sessions.touch(conn, rec.id, now_ms, **sets)
    write_audit(
        conn,
        now_ms=now_ms,
        action="hub_session_wake",
        result="ok",
        detail={"hub_session_id": rec.id, "signals": total, "says": queued},
        **audit,
    )
    return held_work._session(conn, rec.id)  # noqa: SLF001
