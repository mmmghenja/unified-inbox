"""Тонкий клиент хука движка к сокету хоста хаба (Ф71-1 для Cursor, Ф74-5 для Codex).

Хук сам ничего не решает: вход хука (JSON из stdin) уходит ХОСТУ одной строкой `{"event", "input"}` по
абстрактному unix-сокету процесса сессии, ответ хоста — одна строка JSON (её формат — дело движка: Cursor и
Codex отличаются только им). Хост может держать ответ сколько угодно (карточка владельцу); хук ждёт.

Подлинность сервера: `SO_PEERCRED` собеседника — pid хоста (переменная `pid_env`) и свой uid. Любой сбой (нет
переменных, нет соединения, не тот собеседник, хост закрыл соединение без ответа, неразборчивый вход или
ответ) — `Refused`: вызывающий печатает запрет.

Только stdlib: хук зовётся на каждый инструмент."""

import json
import os
import socket
import struct

INPUT_MAX = 64 * 1024 * 1024  # `Write` несёт файл целиком
REPLY_MAX = 64 * 1024
_UCRED = struct.Struct("3i")


class Refused(Exception):
    pass


def ask_host(
    event: str, raw: bytes, environ, *, sock_env: str, pid_env: str, timeout_s: float | None = None
) -> dict:
    """Ответ хоста (объект JSON) на вход хука. Не вышло — `Refused` (или исключение разбора)."""
    if len(raw) > INPUT_MAX:
        raise Refused("вход хука слишком велик")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise Refused("вход хука — не объект")
    name = environ.get(sock_env) or ""
    pid = int(environ.get(pid_env) or "0")
    if not name or pid <= 0:
        raise Refused("нет адреса хоста")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC) as sock:
        if timeout_s is not None:
            sock.settimeout(timeout_s)
        sock.connect("\0" + name)
        peer_pid, peer_uid, _gid = _UCRED.unpack(
            sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size)
        )
        if peer_pid != pid or peer_uid != os.getuid():
            raise Refused("на сокете не хост хаба")
        line = json.dumps({"event": event, "input": data}, ensure_ascii=False).encode("utf-8") + b"\n"
        # Без `shutdown(SHUT_WR)`: конец ввода у хоста значит «хук ушёл» — карточка снимается.
        sock.sendall(line)
        reply = b""
        while b"\n" not in reply and len(reply) <= REPLY_MAX:
            chunk = sock.recv(REPLY_MAX + 1)
            if not chunk:
                break
            reply += chunk
    if b"\n" not in reply:
        raise Refused("хост закрыл соединение без ответа")
    answer = json.loads(reply.split(b"\n", 1)[0].decode("utf-8"))
    if not isinstance(answer, dict):
        raise Refused("ответ хоста неразборчив")
    return answer
