"""Ф77-1 (docs/architecture.md): реестр машин, с которых сессии ходят к ядру.

Машина — `host` (16 hex `hub.hostid`, как `session_host` подписок) -> имя от владельца, `hostname`, путь до ядра
(`route`), версия хаба, первый и последний контакт (таблица `main.machine`, миграция `0107_main.sql`).

Машина подписки не хранится заново — вычисляется (`of_session`): `session_host` строки, а пустой
`session_host` и сессия хаба (условный pid <= -1) — машина ядра. После переключения главной (Ф33) машина
сессий хаба меняется сама: «машина ядра» — это `hostid` процесса ядра (`session_host.own()`).

- `register` — контакт клиента (тело `POST /subs`, `POST /chan-subs`, `POST /subs/beat` с `host` в
  `session`); `name` не трогает; `last_ts` — не чаще раза в `TOUCH_MS` на машину (по строке базы), прочие
  поля — при изменении. Своя машина ядра — всегда `route = core`, `hostname` и версия — только от
  `register_core` (клиент на машине ядра может быть из другого рабочего дерева).
- `register_core` — при старте ядра: своя строка; у прежней машины ядра (переезд) `route = core` снимается.
- `label` / `Labels` — метка для выдачи: `{host, name, hostname, display, route, core, online, last_ts,
  version}`. Хеш `host` — не секрет (это не `machine-id`): отдаётся всем токенам.
- `rename` — имя от владельца (только кука интерфейса и внутренний сокет)."""

from __future__ import annotations

import socket

from .. import buildinfo, hostid, machine_info
from . import session_host as session_host_mod

TOUCH_MS = 60_000  # `last_ts` — не чаще раза в минуту на машину
ONLINE_MS = 180_000  # контакт моложе 3 мин — «на связи» (пульс MCP — раз в 60 с)
NAME_MAX = 32
HOSTNAME_MAX = machine_info.HOSTNAME_MAX
CORE = "core"
ROUTES = (CORE, *machine_info.ROUTES)
ROUTE_RU = {
    CORE: "машина ядра",
    "lan": "локальная сеть",
    "s1": "через relay",
    "s1-socks": "через relay (socks)",
}
_COLUMNS = "host, hostname, name, route, version, first_ts, last_ts"


class MachineError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def core_host() -> str | None:
    """Машина ядра: реестр сессий приложения (`session_host.install`), без приложения — `hostid` процесса."""
    return session_host_mod.own() or hostid.host_id()


def _valid_host(host) -> bool:
    return isinstance(host, str) and bool(hostid.HOST_RE.fullmatch(host))


def of_session(host, pid) -> str | None:
    """Машина подписки: сессия хаба (pid <= -1) и пустой `host` — машина ядра, иначе `host`."""
    if isinstance(pid, int) and not isinstance(pid, bool) and pid <= -1:
        return core_host()
    return host if _valid_host(host) else core_host()


def _row(conn, host: str):
    return conn.execute(
        f"SELECT {_COLUMNS} FROM main.machine WHERE host = ?",  # noqa: S608 — константа
        (host,),
    ).fetchone()


def _upsert(
    conn, host: str, *, hostname, route, version, now_ms: int, reported: bool, force_touch: bool = False
) -> bool:
    row = _row(conn, host)
    if row is None:
        conn.execute(
            "INSERT INTO main.machine (host, hostname, name, route, version, first_ts, last_ts) "
            "VALUES (?, ?, NULL, ?, ?, ?, ?)",
            (host, hostname if reported else None, route, version if reported else None, now_ms, now_ms),
        )
        return True
    _host, old_hostname, _name, old_route, old_version, _first, last_ts = row
    sets: dict = {}
    if reported:
        if hostname != old_hostname:
            sets["hostname"] = hostname
        if version != old_version:
            sets["version"] = version
    if route != old_route and (reported or route == CORE):
        sets["route"] = route
    if force_touch or sets or now_ms - (last_ts or 0) >= TOUCH_MS:
        sets["last_ts"] = max(now_ms, last_ts or 0)
    if not sets:
        return False
    assign = ", ".join(f"{column} = ?" for column in sets)
    conn.execute(
        f"UPDATE main.machine SET {assign} WHERE host = ?",  # noqa: S608 — имена колонок заданы кодом
        (*sets.values(), host),
    )
    return True


def register(
    conn,
    host,
    *,
    hostname: str | None = None,
    route: str | None = None,
    version: str | None = None,
    now_ms: int,
    reported: bool = True,
) -> bool:
    """Контакт клиента с машины `host`. `reported=False` — старый клиент без поля `machine`: строка
    появляется (или освежается) без `hostname`, пути и версии, прежние значения не стираются. Машина ядра —
    `route = core` при любом присланном, `hostname`/версия — не от клиента. Записано ли что-нибудь."""
    if not _valid_host(host):
        return False
    if host == core_host():
        return _upsert(conn, host, hostname=None, route=CORE, version=None, now_ms=now_ms, reported=False)
    hostname = machine_info.clean_hostname(hostname)
    route = route if route in machine_info.ROUTES else None
    version = version if isinstance(version, str) and buildinfo.COMMIT_RE.fullmatch(version) else None
    return _upsert(
        conn, host, hostname=hostname, route=route, version=version, now_ms=now_ms, reported=reported
    )


def register_from_body(conn, session, machine, *, now_ms: int) -> bool:
    """Регистрация по телу ручки: `session` — модель или словарь с `host`; `machine` — модель `MachineIn`,
    словарь или `None` (старый клиент). Без `host` в `session` — машина ядра, ничего не пишется."""
    host = session_host_mod.host_of(session)
    if host is None:
        return False
    if machine is None:
        return register(conn, host, now_ms=now_ms, reported=False)
    get = machine.get if isinstance(machine, dict) else lambda k: getattr(machine, k, None)
    return register(
        conn, host, hostname=get("hostname"), route=get("route"), version=get("version"), now_ms=now_ms
    )


def register_core(conn, *, now_ms: int, host: str | None = None) -> bool:
    """Старт ядра: своя машина (`hostid`, `socket.gethostname()`, `route = core`, версия `buildinfo`). У
    прежней машины ядра (переключатель главной) отметка `core` снимается — её путь теперь неизвестен до
    первого контакта."""
    host = host if host is not None else hostid.host_id()
    if not _valid_host(host):
        return False
    try:
        name = socket.gethostname()
    except OSError:
        name = None
    conn.execute("UPDATE main.machine SET route = NULL WHERE route = ? AND host <> ?", (CORE, host))
    return _upsert(
        conn,
        host,
        hostname=machine_info.clean_hostname(name),
        route=CORE,
        version=buildinfo.commit(),
        now_ms=now_ms,
        reported=True,
        force_touch=True,
    )


def _display(host: str | None, name, hostname) -> str:
    if name:
        return name
    if hostname:
        return hostname
    return f"машина {host[:6]}" if host else "машина ядра"


def _label(row, host: str | None, core: str | None, now_ms: int | None) -> dict:
    is_core = host is None or host == core
    if row is None:
        hostname = name = route = version = last_ts = None
        if is_core:
            try:
                hostname = machine_info.clean_hostname(socket.gethostname())
            except OSError:
                hostname = None
    else:
        _h, hostname, name, route, version, _first, last_ts = row
    if is_core:
        route = CORE
    elif route == CORE:
        route = None  # прежняя машина ядра после переезда
    online = is_core or (last_ts is not None and now_ms is not None and 0 <= now_ms - last_ts <= ONLINE_MS)
    return {
        "host": host,
        "name": name,
        "hostname": hostname,
        "display": _display(host, name, hostname),
        "route": route,
        "core": is_core,
        "online": bool(online),
        "last_ts": last_ts,
        "version": version,
    }


def label(conn, host, *, now_ms: int | None = None) -> dict:
    """Метка машины `host` (`None` — машина ядра)."""
    core = core_host()
    host = host if _valid_host(host) else core
    row = _row(conn, host) if host is not None else None
    return _label(row, host, core, now_ms)


class Labels:
    """Метки одного чтения (списки подписок, очередь владельца): строки машин читаются раз."""

    def __init__(self, conn, *, now_ms: int | None = None) -> None:
        self.conn = conn
        self.now_ms = now_ms
        self.core = core_host()
        self._rows: dict | None = None

    def _all(self) -> dict:
        if self._rows is None:
            rows = self.conn.execute(f"SELECT {_COLUMNS} FROM main.machine").fetchall()  # noqa: S608
            self._rows = {row[0]: row for row in rows}
        return self._rows

    def of_host(self, host) -> dict:
        host = host if _valid_host(host) else self.core
        return _label(self._all().get(host) if host is not None else None, host, self.core, self.now_ms)

    def of_session(self, host, pid) -> dict:
        return self.of_host(of_session(host, pid))

    def of_rec(self, rec) -> dict:
        """Подписка на проект или на беседу (`session_host`, `session_pid`)."""
        return self.of_session(rec.session_host, rec.session_pid)

    def core_label(self) -> dict:
        return self.of_host(self.core)


def of_rec(conn, rec, *, now_ms: int | None = None) -> dict:
    return Labels(conn, now_ms=now_ms).of_rec(rec)


def of_sub_id(conn, sub_id, *, now_ms: int | None = None) -> dict | None:
    """Машина подписки на проект по номеру (отправитель сообщения агентов); нет подписки — `None`."""
    if not isinstance(sub_id, int):
        return None
    row = conn.execute(
        "SELECT session_host, session_pid FROM main.session_sub WHERE id = ?", (sub_id,)
    ).fetchone()
    if row is None:
        return None
    return label(conn, of_session(row[0], row[1]), now_ms=now_ms)


def list_all(conn, *, now_ms: int | None = None) -> list[dict]:
    """Все машины: машина ядра первой, дальше — по последнему контакту (новые выше)."""
    labels = Labels(conn, now_ms=now_ms)
    hosts = list(labels._all())  # noqa: SLF001 — свой класс
    if labels.core is not None and labels.core not in hosts:
        hosts.append(labels.core)
    items = [labels.of_host(h) for h in hosts]
    items.sort(key=lambda m: (not m["core"], -(m["last_ts"] or 0), m["host"] or ""))
    return items


def clean_name(raw) -> str | None:
    """Имя от владельца: пусто — снять (`None`); 1–32 печатных символа; иначе `MachineError(422)`."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise MachineError(422, "name: строка")
    text = raw.strip()
    if not text:
        return None
    if len(text) > NAME_MAX or not text.isprintable():
        raise MachineError(422, f"name: 1–{NAME_MAX} печатных символов")
    return text


def rename(conn, host, name, *, now_ms: int) -> dict:
    """Задать или снять (пустая строка) имя машины. Нет такой машины — 404; машина ядра без строки
    (ядро ещё не записало себя) заводится."""
    if not _valid_host(host):
        raise MachineError(404, "нет такой машины")
    value = clean_name(name)
    if _row(conn, host) is None:
        if host != core_host():
            raise MachineError(404, "нет такой машины")
        register_core(conn, now_ms=now_ms, host=host)
    conn.execute("UPDATE main.machine SET name = ? WHERE host = ?", (value, host))
    return label(conn, host, now_ms=now_ms)


def route_ru(label_: dict | None) -> str | None:
    """Путь до ядра словами: «машина ядра» / «локальная сеть» / «через relay» / «через relay (socks)»; неизвестен —
    `None`."""
    if not isinstance(label_, dict):
        return None
    if label_.get("core"):
        return ROUTE_RU[CORE]
    return ROUTE_RU.get(label_.get("route"))
