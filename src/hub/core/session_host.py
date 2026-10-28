"""Ф33-1 (docs/architecture.md и 12а К1, К2, К9, К10): живость сессии на другой машине.

Ядро переезжает на другую машину, внешние сессии Claude Code и OpenCode остаются на ноутбуке и ходят к ядру
по туннелю. Их процессов в `/proc` ядра нет — живость берётся из реестра в памяти ядра: личность
`(host, pid, proc_start)` -> последний контакт, идущие ожидания, `gone`, кэш стенограммы.

- Клиент кладёт в тело `session` поле `host` (`hub.hostid`), строка подписки хранит его в `session_host`.
  Строку с пустым `session_host` или машиной ядра реестр НЕ судит никогда — живость по `/proc`, как до 33-1
  (ревью Б1: тело с чужим `host` не превращает локальную сессию в удалённую). Вызывающий передаёт машину
  строки (`subs.is_alive(pid, proc_start, host)`).
- Любой запрос с телом `session` и чужим `host` — контакт (валидатор моделей `SessionIn`/`SendSessionIn`
  зовёт `touch`; зависимость принципала FastAPI решает раньше тела, поэтому без токена контакта нет).
  Пульс MCP-сервера сессии (`POST /subs/beat`, раз в 60 с) — такой же контакт плюс кэш и контекст.
- Состояние (`state`): `gone` — `DEAD`; без связи дольше `DROP_MS` — `DEAD`; OpenCode — иначе `LIVE` (процесс
  не проверяется, брошенную подписку снимает `subs.oc_stale`); идёт ожидание или контакт (или старт ядра)
  моложе `STALE_MS` — `LIVE`; иначе `STALE` («не в сети»: сигналы копятся, подписка НЕ снимается, К2).
- `subs.is_alive` — `LIVE`; `subs.session_alive` (разрушающие решения) — не `DEAD`.
- Последний контакт сохраняется в строках подписок (`session_seen_ts`, не чаще раза в `SAVE_MS`). После старта
  ядра реестр засевается живыми строками с чужой машиной: окно 180 с — от старта, окно 12 ч — от последнего
  контакта строки (`session_seen_ts`, `created_ts`, `last_wait_ts`, `last_delivery_ts` — наибольшее): рестарты
  ядра мёртвую подписку не продлевают (ревью В1).
- Правка финального ревью (17.10): контакт пишется и у сессии СВОЕЙ машины (строка с `session_host` == машина
  ядра; подписка, пульс, ожидание, тело `session`; плюс чистка, увидевшая живой процесс в `/proc`) — после
  переезда ядра эта машина становится чужой, и окно 12 ч считается от последнего контакта, а не от создания
  строки. Своя машина реестром по-прежнему не судится (`state` — `None`): здесь только отметка для записи.

Реестр — один на базу (как `held`): публичное и внутреннее приложения над одной `Db` получают тот же объект.
Часы — часы приложения (секунды, как `app.state.clock`)."""

from __future__ import annotations

import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass

from .. import hostid
from . import engine as engine_mod

STALE_MS = 180_000  # нет ожидания и контакта 3 мин — «не в сети»
DROP_MS = 12 * 60 * 60 * 1000  # 12 ч без связи — сессии нет
SAVE_MS = 10 * 60 * 1000  # контакт в строки подписок — не чаще раза в 10 мин
LIVE = "live"
STALE = "stale"
DEAD = "dead"
HOST_PATTERN = r"^[0-9a-f]{16}$"  # поле `host` тела `session` (`hub.hostid.HOST_RE`)


@dataclass
class Ident:
    oc: bool
    last_ms: int  # последний настоящий контакт (засев — сохранённый в строке)
    waits: int = 0
    gone: bool = False
    # Кэш и контекст стенограммы из пульса (форма `transcript.state_for`) или `None` (не присылали).
    cache: dict | None = None
    # Ф99-7.3: хвост стенограммы из пульса (события ленты, уже маскированные) и время его получения.
    tail: list | None = None
    tail_ms: int = 0
    boot: str | None = None  # загрузка машины личности; `None` — клиент не прислал
    grace_ms: int = 0  # старт ядра (засев): окно `STALE_MS` от него
    saved_ms: int = 0  # последний контакт, записанный в строки подписок


Key = tuple[str, int, int]


class Registry:
    def __init__(self, own: str | None = None, clock: Callable[[], float] = time.time) -> None:
        self.own = own
        self.clock = clock
        self._ids: dict[Key, Ident] = {}
        # Сессии своей машины (с `host` == машина ядра): `[последний контакт, записанный в строки]`. Только
        # для записи `session_seen_ts`, живость их — по `/proc`.
        self._local: dict[Key, list[int]] = {}

    def now_ms(self) -> int:
        return int(self.clock() * 1000)

    def foreign(self, host) -> bool:
        """`host` — чужая машина: непустая строка и не машина ядра."""
        return isinstance(host, str) and bool(host) and host != self.own

    def local(self, host, pid=1) -> bool:
        """`host` — машина ядра, названная явно (непустая), и настоящий pid: контакт такой сессии пишется в её
        строки (`session_seen_ts`) — после переезда ядра машина станет чужой."""
        return isinstance(host, str) and bool(host) and host == self.own and isinstance(pid, int) and pid > 0

    def _local_contact(self, host, pid, proc_start, now: int) -> None:
        if not self.local(host, pid) or not isinstance(proc_start, int):
            return
        mark = self._local.setdefault((host, pid, proc_start), [now, 0])
        mark[0] = max(mark[0], now)

    def touch(
        self, pid, proc_start, session_id, host, *, boot: str | None = None, now_ms: int | None = None
    ) -> bool:
        """Контакт личности с чужой машины; своя машина, нет `host`, условный pid сессии хаба — `False`,
        реестр не судит (у своей машины, названной явно, — только отметка контакта для `session_seen_ts`).
        `gone` контактом не снимается: процесс проверенно умер, запоздавший запрос его не воскрешает.
        Та же пара (pid, starttime) из другой загрузки — новая личность."""
        if not isinstance(pid, int) or pid <= 0 or not isinstance(proc_start, int) or not self.foreign(host):
            self._local_contact(host, pid, proc_start, self.now_ms() if now_ms is None else now_ms)
            return False
        boot = boot if isinstance(boot, str) and boot else None
        now = self.now_ms() if now_ms is None else now_ms
        oc = _idle_judged(session_id)
        key = (host, pid, proc_start)
        ident = self._ids.get(key)
        if ident is None or (boot and ident.boot and ident.boot != boot):
            self._ids[key] = Ident(oc=oc, last_ms=now, boot=boot)
        else:
            ident.last_ms = max(ident.last_ms, now)
            ident.oc = ident.oc or oc
            ident.boot = ident.boot or boot
        return True

    def get(self, host, pid, proc_start) -> Ident | None:
        if not self.foreign(host):
            return None
        return self._ids.get((host, pid, proc_start))

    def wait_begin(self, host, pid, proc_start) -> bool:
        ident = self.get(host, pid, proc_start)
        if ident is None:
            self._local_contact(host, pid, proc_start, self.now_ms())
            return False
        ident.waits += 1
        ident.last_ms = max(ident.last_ms, self.now_ms())
        return True

    def wait_end(self, host, pid, proc_start) -> None:
        ident = self.get(host, pid, proc_start)
        if ident is None:
            return
        ident.waits = max(0, ident.waits - 1)
        ident.last_ms = max(ident.last_ms, self.now_ms())  # «ожидание закрыто моложе 180 с» — тоже жива

    def mark_gone(self, host, pid, proc_start) -> bool:
        ident = self.get(host, pid, proc_start)
        if ident is None:
            return False
        ident.gone = True
        return True

    def set_cache(self, host, pid, proc_start, cache: dict | None) -> None:
        ident = self.get(host, pid, proc_start)
        if ident is not None:
            ident.cache = cache

    def set_tail(self, host, pid, proc_start, tail: list | None, now_ms: int) -> None:
        ident = self.get(host, pid, proc_start)
        if ident is not None and tail is not None:
            ident.tail = tail
            ident.tail_ms = now_ms

    def mark_saved(self, host, pid, proc_start, seen_ms: int) -> None:
        """Строка подписки записана с контактом `seen_ms` (новая подписка) — следующая запись через
        `SAVE_MS`."""
        ident = self.get(host, pid, proc_start)
        if ident is not None:
            ident.saved_ms = max(ident.saved_ms, seen_ms)
        elif self.local(host, pid) and isinstance(proc_start, int):
            mark = self._local.setdefault((host, pid, proc_start), [seen_ms, 0])
            mark[0] = max(mark[0], seen_ms)
            mark[1] = max(mark[1], seen_ms)

    def to_save(self, host, pid, proc_start) -> int | None:
        """Контакт, который пора записать в строки подписок (`session_seen_ts`), или `None` (рано, личности
        нет). Отметка «записано» ставится сразу: запись — в той же транзакции вызывающего."""
        ident = self.get(host, pid, proc_start)
        if ident is None:
            mark = self._local.get((host, pid, proc_start)) if self.local(host, pid) else None
            if mark is None or mark[0] - mark[1] < SAVE_MS:
                return None
            mark[1] = mark[0]
            return mark[0]
        if ident.gone or ident.last_ms - ident.saved_ms < SAVE_MS:
            return None
        ident.saved_ms = ident.last_ms
        return ident.last_ms

    def state(self, host, pid, proc_start, *, now_ms: int | None = None) -> str | None:
        """`LIVE` / `STALE` / `DEAD` личности с чужой машины; своя машина или реестр её не знает — `None`."""
        ident = self.get(host, pid, proc_start)
        if ident is None:
            return None
        if ident.gone:
            return DEAD
        now = self.now_ms() if now_ms is None else now_ms
        if now - ident.last_ms > DROP_MS and ident.waits == 0:
            return DEAD
        if ident.oc:
            # К9: у OpenCode процесс не проверяется — брошенную подписку снимает `oc_stale`.
            return LIVE
        if ident.waits > 0 or now - max(ident.last_ms, ident.grace_ms) <= STALE_MS:
            return LIVE
        return STALE

    def seed(self, conn, *, now_ms: int) -> None:
        """Старт ядра: живые подписки (на проект и на беседы) с чужой машиной — в реестр. Окно 180 с — от
        старта (К2), окно 12 ч — от последнего контакта строки: наибольшее из `session_seen_ts`, `created_ts`,
        `last_wait_ts` (ожидание закрылось), `last_delivery_ts` (сигнал отдан) — у строк, созданных до записи
        контакта своей машины (правка 17.10), `session_seen_ts` пуст, и переезд ядра не должен их хоронить."""
        for table in ("session_sub", "session_chan_sub"):
            rows = conn.execute(
                "SELECT session_pid, session_proc_start, session_id, session_host, session_boot, "  # noqa: S608
                "MAX(created_ts, COALESCE(session_seen_ts, 0), COALESCE(last_wait_ts, 0), "
                f"COALESCE(last_delivery_ts, 0)) FROM main.{table} "
                "WHERE ended_ts IS NULL AND session_host IS NOT NULL"
            ).fetchall()
            for pid, proc_start, session_id, host, boot, seen in rows:
                if not self.foreign(host):
                    continue
                key = (host, pid, proc_start)
                ident = self._ids.get(key)
                if ident is None:
                    oc = _idle_judged(session_id)
                    self._ids[key] = Ident(
                        oc=oc, last_ms=seen, boot=boot, grace_ms=now_ms, saved_ms=seen
                    )
                elif seen > ident.last_ms:
                    ident.last_ms = ident.saved_ms = seen

    def prune(self, *, now_ms: int) -> None:
        """Забыть личности без связи дольше `2 * DROP_MS` (и `gone` той же давности): их подписки к этому
        времени сняты чисткой, а реестр не должен расти без конца."""
        for key, ident in list(self._ids.items()):
            if ident.waits == 0 and now_ms - max(ident.last_ms, ident.grace_ms) > 2 * DROP_MS:
                del self._ids[key]
        for key, mark in list(self._local.items()):
            if now_ms - mark[0] > 2 * DROP_MS:
                del self._local[key]

    def __len__(self) -> int:
        return len(self._ids)


# Модульная ссылка для `subs.is_alive` и валидаторов моделей (у них нет ни соединения, ни приложения). Без
# приложения реестр пуст — все сессии свои (как до Ф33-1).
_current = Registry()
_by_db: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def install(db, clock: Callable[[], float] = time.time) -> Registry:
    """Сборка приложения: реестр базы `db` (один на базу) с машиной ядра и часами приложения, засеянный живыми
    подписками чужой машины, становится текущим — до фоновых задач ядра."""
    global _current  # noqa: PLW0603 — единственная точка замены текущего реестра
    registry = _by_db.get(db)
    if registry is None:
        registry = _by_db[db] = Registry(hostid.host_id(), clock)
    with db.reader() as conn:
        registry.seed(conn, now_ms=int(clock() * 1000))
    _current = registry
    return registry


def reset() -> None:
    """Тесты: как процесс без приложения — реестр пуст."""
    global _current  # noqa: PLW0603
    _current = Registry()


def current() -> Registry:
    return _current


def own() -> str | None:
    return _current.own


def host_of(session) -> str | None:
    """Поле `host` тела сессии (словарь или модель); нет — `None`."""
    if session is None:
        return None
    if isinstance(session, dict):
        return session.get("host")
    return getattr(session, "host", None)


def _idle_judged(session_id) -> bool:
    """К9 + Ф74-2 (В2): процесс личности не проверяется — брошенную подписку снимает `subs.oc_stale`
    (OpenCode, внешние Codex и Cursor: у профиля движка есть `idle_grace_ms`; поле `Ident.oc`)."""
    return engine_mod.idle_grace_ms(engine_mod.engine_of_session(None, session_id)) is not None


def touch_session(session) -> bool:
    """Контакт по телу `session` (модель или словарь с `pid`, `proc_start`, `session_id`, `host`, `boot`)."""
    get = session.get if isinstance(session, dict) else lambda k: getattr(session, k, None)
    return _current.touch(get("pid"), get("proc_start"), get("session_id"), get("host"), boot=get("boot"))


def seen_at_insert(session: dict, now_ms: int) -> int | None:
    """`session_seen_ts` новой строки подписки: у сессии с названной машиной (другой или своей, правка
    17.10) — `now_ms` (и отметка «записано» в реестре); без машины или у сессии хаба — `None`."""
    host = session.get("host")
    if not _current.foreign(host) and not _current.local(host, session.get("pid")):
        return None
    _current.mark_saved(host, session.get("pid"), session.get("proc_start"), now_ms)
    return now_ms


def state(host, pid, proc_start) -> str | None:
    return _current.state(host, pid, proc_start)


def cache_of(host, pid, proc_start) -> tuple[bool, dict | None]:
    """`(удалённая ли, кэш из пульса)`: у своей сессии — `(False, None)`, ядро читает стенограмму само."""
    ident = _current.get(host, pid, proc_start)
    if ident is None:
        return False, None
    return True, ident.cache


def tail_of(host, pid, proc_start) -> tuple[bool, list | None, int]:
    """Ф99-7.3: `(удалённая ли, хвост из пульса, когда получен)`; у своей сессии — `(False, None, 0)`."""
    ident = _current.get(host, pid, proc_start)
    if ident is None:
        return False, None, 0
    return True, ident.tail, ident.tail_ms


def cache_from_beat(raw: dict) -> dict | None:
    """Поля пульса -> форма `transcript.state_for` (её ждёт интерфейс): `cache_until_ms` — `expires_ts`."""
    until = raw.get("cache_until_ms")
    tokens = raw.get("context_tokens")
    if until is None and tokens is None:
        return None
    ttl = raw.get("ttl_s")
    ttl = ttl if isinstance(ttl, int) and ttl > 0 else 3600
    model = raw.get("model")
    return {
        "last_ts": until - ttl * 1000 if until is not None else None,
        "ttl_s": ttl,
        "expires_ts": until if until is not None else 0,
        "context_tokens": tokens if tokens is not None else 0,
        "model": model if isinstance(model, str) else "",
    }
