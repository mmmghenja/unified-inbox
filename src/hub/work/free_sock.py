"""Набор снятых выключателей автопилота — гарду по сокету (Ф50-3, поправка по ревью; план
`docs/architecture.md`).

Набор живёт только в памяти хоста: файла, до которого дотянулся бы процесс сессии (тот же пользователь —
права на файл не спасают), нет. Хост держит ОДИН unix-сокет в абстрактном пространстве имён Linux (имя
начинается с нулевого байта: в файловой системе его нет, удалить или перепривязать его, пока хост жив,
нельзя): `hub-work-free-<pid хоста>-<случайный хвост>`.

Протокол: клиент (хук `hub-work-guard`) шлёт строку `<id сессии хаба>\\n`, сервер отвечает одной строкой —
JSON-списком ключей правил — и закрывает соединение. Неизвестная сессия, мусор в запросе, сбой — `[]`.
Отвечает отдельный поток (не цикл событий хоста: занятый цикл не должен превращаться в карточки) из памяти,
без похода в ядро. Подлинность сервера сверяет клиент: `SO_PEERCRED` — pid и uid (`guard.read_free_sock`).
"""

import contextlib
import json
import logging
import os
import secrets
import socket
import threading

_logger = logging.getLogger(__name__)

REQUEST_MAX = 32  # id сессии — короткое целое
CLIENT_TIMEOUT_S = 2.0
BACKLOG = 64


class FreeServer:
    """Сервер набора: `lookup(sid) -> итерируемое ключей` зовётся из потока сервера на каждый запрос."""

    def __init__(self, lookup) -> None:
        self._lookup = lookup
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self.name: str | None = None
        self.pid = os.getpid()

    def start(self) -> None:
        """Занять имя и начать отвечать; повторный вызов — ничего. Не вышло — `OSError`."""
        if self._sock is not None:
            return
        name = f"hub-work-free-{self.pid}-{secrets.token_hex(8)}"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        try:
            sock.bind("\0" + name)
            sock.listen(BACKLOG)
        except OSError:
            sock.close()
            raise
        self._sock, self.name = sock, name
        self._thread = threading.Thread(target=self._serve, args=(sock,), name="hub-free-sock", daemon=True)
        self._thread.start()

    def close(self) -> None:
        sock, self._sock, self.name = self._sock, None, None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)  # будит `accept` потока
            sock.close()

    def args(self, sid: int) -> list[str]:
        """Аргументы хука для сессии `sid`; сервер не запущен — пусто (гард возьмёт пустой набор)."""
        if self.name is None:
            return []
        return ["--free-sock", self.name, "--free-pid", str(self.pid), "--free-session", str(int(sid))]

    def _serve(self, sock: socket.socket) -> None:
        while True:
            try:
                conn, _addr = sock.accept()
            except OSError:
                return  # сокет закрыт
            threading.Thread(
                target=self._answer, args=(conn,), name="hub-free-sock-conn", daemon=True
            ).start()

    def _answer(self, conn: socket.socket) -> None:
        try:
            with conn:
                conn.settimeout(CLIENT_TIMEOUT_S)
                raw = b""
                while b"\n" not in raw and len(raw) <= REQUEST_MAX:
                    chunk = conn.recv(REQUEST_MAX + 1)
                    if not chunk:
                        break
                    raw += chunk
                conn.sendall(json.dumps(self._keys(raw)).encode("ascii") + b"\n")
        except Exception as exc:  # noqa: BLE001 — клиент без ответа берёт пустой набор
            _logger.debug("work: сокет набора автопилота — сбой ответа (%s)", exc.__class__.__name__)

    def _keys(self, raw: bytes) -> list[str]:
        line = raw.split(b"\n", 1)[0]
        if b"\n" not in raw or not line.isdigit() or len(line) > REQUEST_MAX:
            return []
        try:
            return sorted(k for k in self._lookup(int(line)) if isinstance(k, str))
        except Exception as exc:  # noqa: BLE001 — сбой поиска: строгая сторона
            _logger.error("work: сокет набора автопилота — сбой поиска (%s)", exc.__class__.__name__)
            return []
