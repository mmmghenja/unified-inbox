"""CLI `hub …`. Спецификация: docs/architecture.md.

Все подкоманды, КРОМЕ `hub core`, ходят ТОЛЬКО во внутренний сокет через `httpx.Client` поверх
`HTTPTransport(uds=...)` — модуль `sqlite3` этот файл не импортирует и `Db` не создаёт. `hub core` —
единственное исключение: она поднимает `runner.run_core`, который и открывает БД (импорт `runner` —
внутри самой подкоманды, лениво, чтобы остальные команды его не тянули).
"""

import argparse
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

from hub import __version__

from . import backup_recipient, hostid, load_text, paths, trace_text
from .core import connector_auth

_INTERNAL_BASE_URL = "http://hub-internal/internal/v1"
_PUBLIC_PORT = 8787  # hub.core.app_public.DEFAULT_PORT — не импортируем модуль ради константы в CLI-путях
_ADMIN_KEY_NAME = "admin.key"
_SOCKET_NAME = "internal.sock"


def _default_ui_dir() -> Path | None:
    """`<корень репозитория>/ui/dist`, если он существует. Корень — от `__file__` (`src/hub/cli.py` ->
    `parents[2]`): при установке пакета не в editable-режиме этого каталога не будет вовсе, и это ожидаемо
    даёт `None` (спека, раздел «6a. Python-часть»)."""
    candidate = Path(__file__).resolve().parents[2] / "ui" / "dist"
    return candidate if candidate.is_dir() else None


def _admin_client(*, timeout: float | None = None) -> httpx.Client:
    key_path = paths.config_dir() / _ADMIN_KEY_NAME
    if not key_path.exists():
        raise SystemExit(f"{key_path} не найден — запустите `hub core` хотя бы раз")
    secret = key_path.read_text(encoding="utf-8").strip()

    socket_path = paths.data_dir() / _SOCKET_NAME
    if not socket_path.exists():
        raise SystemExit(f"внутренний сокет {socket_path} не найден — `hub core` не запущен?")

    transport = httpx.HTTPTransport(uds=str(socket_path))
    kwargs = {} if timeout is None else {"timeout": timeout}
    return httpx.Client(
        transport=transport,
        base_url=_INTERNAL_BASE_URL,
        headers={"Authorization": f"Bearer {secret}"},
        **kwargs,
    )


def _cmd_core(args: argparse.Namespace) -> None:
    from .core import runner  # лениво: только эта подкоманда трогает Db/sqlite3

    ui_dir = args.ui_dir if args.ui_dir is not None else _default_ui_dir()
    runner.run_core(
        paths.data_dir(), paths.config_dir(), port=args.port, ui_dir=ui_dir, remote_port=args.remote_port
    )


def _cmd_ui(args: argparse.Namespace) -> None:
    with _admin_client() as client:
        r = client.post("/admin/login-code")
        r.raise_for_status()
        code = r.json()["code"]

    # Код — во фрагменте (`#code=`), НЕ в query: фрагмент не уходит на сервер, в логи и в Referer (спека).
    url = f"http://127.0.0.1:{_PUBLIC_PORT}/login#code={code}"
    if args.print_url:
        print(url)
    else:
        # Отвязанный процесс без вывода: иначе терминал «висит» на логах Chromium, пока открыто окно.
        subprocess.Popen(  # noqa: S603 — намеренный локальный браузер
            ["chromium", f"--app={url}"],  # noqa: S607
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def _cmd_auth(args: argparse.Namespace) -> None:
    if args.source == "telegram" or args.source == "tg":
        from .connectors.telegram.auth import cmd_auth_tg

        with _admin_client() as client:
            code = cmd_auth_tg(
                args, admin_client=client, config_dir=paths.config_dir(), data_dir=paths.data_dir()
            )
        if code != 0:
            raise SystemExit(code)
        return
    if args.source == "mail":
        from .connectors.mail.auth import cmd_auth_mail

        with _admin_client() as client:
            code = cmd_auth_mail(args, admin_client=client, config_dir=paths.config_dir())
        if code != 0:
            raise SystemExit(code)
        return
    if args.source == "speechkit":
        # Ф59-1: ключ Yandex SpeechKit для диктовки — без эха или из dotenv-файла; ядро не нужно.
        from .speech import auth as speech_auth

        env_file = Path(args.from_env_file).expanduser() if args.from_env_file else None
        code = speech_auth.auth(
            config_dir=paths.config_dir(), env_file=env_file, force=args.force, run_check=args.check
        )
        if code != 0:
            raise SystemExit(code)
        return
    if args.source == "alibaba":
        # Ф91 Р1: ключ Alibaba Model Studio для картинок `hub_image` — без эха или из dotenv-файла.
        from .images import auth as image_auth

        env_file = Path(args.from_env_file).expanduser() if args.from_env_file else None
        code = image_auth.auth(
            config_dir=paths.config_dir(),
            env_file=env_file,
            var=args.var or image_auth.DEFAULT_VAR,
            force=args.force,
            run_check=args.check,
        )
        if code != 0:
            raise SystemExit(code)
        return
    print(f"hub auth {args.source}: появится в фазе 1 — секреты источников вводятся интерактивно.")


def _parse_config_pairs(pairs: list[str] | None) -> dict[str, str] | None:
    if not pairs:
        return None
    config: dict[str, str] = {}
    for pair in pairs:
        key, _, value = pair.partition("=")
        config[key] = value
    return config


def _cmd_account_add(args: argparse.Namespace) -> None:
    config = _parse_config_pairs(args.config)
    with _admin_client() as client:
        r = client.post(
            "/admin/accounts",
            json={"source": args.source, "title": args.title, "db": args.db, "config": config},
        )
        r.raise_for_status()
        body = r.json()

    account_id = body["account_id"]
    key_path = paths.config_dir() / "connectors" / f"{account_id}.key"
    connector_auth.write_secret_file(key_path, body["connector_secret"])
    print(f"account_id={account_id}")
    print(f"секрет коннектора записан в {key_path}")


def _exit_on_client_error(r: httpx.Response, command: str) -> None:
    """Ф14-1 A5: отказ ядра 4xx — текстом `detail` и выходом с ошибкой, без трейсбека."""
    if not 400 <= r.status_code < 500:
        return
    try:
        detail = r.json().get("detail")
    except ValueError:
        detail = None
    if not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False) if detail is not None else f"HTTP {r.status_code}"
    raise SystemExit(f"{command}: {detail}")


def _cmd_project_add(args: argparse.Namespace) -> None:
    with _admin_client() as client:
        r = client.post(
            "/admin/projects", json={"name": args.name, "color": args.color, "workdir": args.workdir}
        )
    _exit_on_client_error(r, "hub project add")
    r.raise_for_status()
    print(f"project_id={r.json()['project_id']}")


def _parse_attention_pairs(pairs: list[str]) -> dict:
    """Ф8-3 A6: `вид=режим` (`dm=react mail_thread=info`); `вид=null` — удалить ключ из политики. Проверку
    видов и режимов делает ядро (422 с понятным текстом)."""
    changes: dict = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key or not value:
            raise SystemExit(f"hub project set --attention: ожидается вид=режим, получено {pair!r}")
        changes[key] = None if value == "null" else value
    return changes


def _parse_unsorted_accounts(value: str) -> list[int]:
    """Ф52-2 A2: `8,5` — номера аккаунтов через запятую, `off` — снять (пустой список). Существование
    аккаунтов проверяет ядро (422)."""
    if value == "off":
        return []
    parts = [part.strip() for part in value.split(",")]
    if not all(part.isdecimal() and int(part) >= 1 for part in parts):
        raise SystemExit("hub project set --unsorted-accounts: номера аккаунтов через запятую (8,5) или off")
    return [int(part) for part in parts]


def _parse_card_route(value: str) -> dict:
    """Ф66-1 (A.6): `группа=значение,…` (`permission=auto,question=agent`); значения — me, agent, auto.
    Группы и допустимость `auto` проверяет ядро (422 с понятным текстом)."""
    changes: dict = {}
    for pair in value.split(","):
        key, sep, route = pair.strip().partition("=")
        if not sep or not key or not route:
            raise SystemExit(f"hub project set --card-route: нужно группа=me|agent|auto, получено {pair!r}")
        changes[key] = route
    return changes


def _cmd_project_set(args: argparse.Namespace) -> None:
    """Спека 7c, W2 + П4: рабочая папка проекта для сессии Claude. Путь — абсолютный, каталог существует, не
    `/`, не `$HOME` и не пересекается с `~/.config`, `~/.local/share`, `~/.claude`, `~/.ssh`, `~/.gnupg` и
    каталогами хаба. Хранится realpath.

    Ф8-3 A6: `--attention вид=режим …` (слияние с политикой проекта)."""
    body: dict = {}
    if args.attention:
        body["attention"] = _parse_attention_pairs(args.attention)
    if args.subs is not None:
        body["subs"] = args.subs == "on"  # Ф13-1 B0: подписки сессий на проект
    if args.session_reactions is not None:
        body["session_reactions"] = args.session_reactions == "on"  # Ф15-1 E: реакции сессий
    if args.agent_msgs is not None:
        body["agent_msgs"] = args.agent_msgs == "on"  # Ф35-2: обмен сообщениями агентов
    if args.reaction_delay is not None:
        from .core.reactions import parse_delay_minutes

        try:
            parse_delay_minutes(args.reaction_delay)
        except ValueError as exc:
            raise SystemExit(f"hub project set --reaction-delay: {exc}") from None
        body["reaction_delay"] = args.reaction_delay
    if args.session_join is not None:
        body["session_join"] = args.session_join  # Ф17-1 E: вступление сессий в группы Telegram
    if args.session_handled is not None:
        body["session_handled"] = args.session_handled == "on"  # Ф37-3 A: отметка «разобрано» сессиями
    if args.digest is not None:
        body["digest_after_compact"] = args.digest == "on"  # Ф86-1 (C4): сводка дня после сжатия
    if args.siblings is not None:
        body["sibling_says"] = args.siblings == "on"  # Ф86-1 (C4): слова владельца из соседних сессий
    if args.agent_batch_min is not None:
        if not 0 <= args.agent_batch_min <= 60:
            raise SystemExit("hub project set --agent-batch-min: минуты, целое 0–60 (0 — сразу)")
        body["agent_batch_min"] = args.agent_batch_min  # Ф48-4: сообщения агентов пачкой
    if args.unsorted_accounts is not None:
        body["unsorted_accounts"] = _parse_unsorted_accounts(args.unsorted_accounts)  # Ф52-2 A2
    if args.workday is not None:
        body["workday"] = args.workday == "on"  # Ф64-1: проект «по времени»
    if args.card_route is not None:
        body["card_route"] = _parse_card_route(args.card_route)  # Ф66-1 (A.6): кто отвечает на карточки
    if args.workdir is None and not args.no_workdir:
        if not body:
            raise SystemExit(
                "hub project set: укажите --workdir/--no-workdir, --attention, "
                "--subs on|off, --session-reactions on|off, --reaction-delay A-B, "
                "--session-join confirm|auto, --session-handled on|off, --agent-msgs on|off, "
                "--agent-batch-min 0–60, --unsorted-accounts N,M|off, --workday on|off, "
                "--digest on|off, --siblings on|off или --card-route группа=me|agent|auto,…"
            )
    else:
        body["workdir"] = _checked_workdir(args)
    with _admin_client() as client:
        r = client.patch(f"/admin/projects/{args.project_id}", json=body)
        if r.status_code == 404:
            raise SystemExit(f"hub project set: проекта {args.project_id} нет")
        if r.status_code == 422:
            raise SystemExit(f"hub project set: {r.json().get('detail')}")
        r.raise_for_status()
    print(json.dumps(r.json(), ensure_ascii=False))


def _checked_workdir(args: argparse.Namespace) -> str | None:
    import os

    from .engine.workdir import unsafe_reason

    workdir = None
    if not args.no_workdir:
        raw = args.workdir
        if not os.path.isabs(raw):
            raise SystemExit(f"hub project set: нужен абсолютный путь, получено {raw!r}")
        if not os.path.isdir(raw):
            raise SystemExit(f"hub project set: каталога {raw} нет")
        reason = unsafe_reason(
            raw, home=Path.home(), config_dir=paths.config_dir(), data_dir=paths.data_dir()
        )
        if reason is not None:
            raise SystemExit(f"hub project set: папку {raw} нельзя отдавать сессии разборщика — {reason}")
        workdir = os.path.realpath(raw)
    return workdir


def _cmd_project_list(_args: argparse.Namespace) -> None:
    """Ф8-3 A6: проекты с политикой внимания — по строке JSON на проект (без счётчиков непрочитанного)."""
    with _admin_client() as client:
        r = client.get("/admin/projects")
        r.raise_for_status()
    for project in r.json():
        if project["id"] < 0:
            continue
        print(
            json.dumps(
                {
                    "id": project["id"],
                    "name": project["name"],
                    "muted": project["muted"],
                    "workdir": project["workdir"],
                    "attention": project.get("attention_policy") or {},
                    "subs": bool(project.get("subs")),  # Ф13-1 B0
                    "session_reactions": project.get("session_reactions"),  # Ф15-1 E
                    "join": project.get("session_join"),  # Ф17-1 E: confirm|auto
                    "session_handled": bool(project.get("session_handled")),  # Ф37-3 A
                    # Ф48-4: пачка сообщений агентов — только когда задана (без неё строка прежняя)
                    **({"agent_batch_min": batch} if (batch := project.get("agent_batch_min")) else {}),
                    # Ф52-2: слышит «Неразобранное» аккаунтов — только когда задано
                    **({"unsorted_accounts": heard} if (heard := project.get("unsorted_accounts")) else {}),
                    # Ф64-1: рабочий день — только у проекта «по времени» (без него строка прежняя)
                    **({"workday": _day_text(project)} if project.get("workday") else {}),
                    # Ф86-1 (C4): выключенные сводка дня после сжатия и слова владельца соседям (включены — строка
                    # прежняя)
                    **({"digest": "off"} if project.get("digest_after_compact") is False else {}),
                    **({"siblings": "off"} if project.get("sibling_says") is False else {}),
                },
                ensure_ascii=False,
            )
        )


def _day_text(project: dict) -> str:
    """Ф64-1: «день открыт» / «день закрыт с ЧЧ:ММ» (время — местное)."""
    from datetime import datetime

    ts = project.get("day_ts")
    if project.get("day_open") is not False:
        return "день открыт"
    stamp = datetime.fromtimestamp(ts / 1000).strftime("%H:%M") if isinstance(ts, int) else "?"
    return f"день закрыт с {stamp}"


def _cmd_project_day(args: argparse.Namespace) -> None:
    """Ф64-1: `hub project day N open|close` — «Начать день» / «Закончить день» (то же, что кнопка в хабе)."""
    with _admin_client() as client:
        r = client.post(f"/admin/projects/{args.project_id}/day", json={"open": args.action == "open"})
        if r.status_code == 404:
            raise SystemExit(f"hub project day: проекта {args.project_id} нет")
        if r.status_code == 409:
            raise SystemExit(
                f"hub project day: проект {args.project_id} не «по времени» — сначала "
                f"hub project set {args.project_id} --workday on"
            )
        r.raise_for_status()
    body = r.json()
    print(_day_text({"day_open": body.get("open"), "day_ts": body.get("ts")}))


# --- hub sub (Ф13-1, E: подписка сессии Claude Code на проект) ----------------------------------------------


def _ext_flags(parser: argparse.ArgumentParser) -> None:
    """Ф74-2: личность внешнего Codex/Cursor для `hub sub wait|leave` (подставляет хук хаба)."""
    parser.add_argument("--ext-session", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--ext-dir", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--ext-pid", type=int, default=None, help=argparse.SUPPRESS)


def _cmd_sub(args: argparse.Namespace) -> None:
    if args.sub_command == "leave":
        from .sub_wait import EXIT_DENIED as LEAVE_DENIED
        from .sub_wait import run_leave

        oc_given = args.oc_session is not None and args.oc_dir is not None
        ext_given = args.ext_session is not None and args.ext_dir is not None
        if oc_given == ext_given:
            print("Хаб: нужна сессия — --oc-session и --oc-dir (OpenCode) или --ext-session и --ext-dir.")
            raise SystemExit(LEAVE_DENIED)

        # Ф16-4 (П1): плагин хаба на `session.deleted` — снять все живые подписки удалённой сессии OpenCode.
        code = run_leave(
            profile=args.profile,
            url=args.url,
            config_dir=paths.config_dir(),
            oc_session=args.oc_session,
            oc_dir=args.oc_dir,
            out=sys.stdout,
            err=sys.stderr,
            ext_session=args.ext_session,  # Ф74-2: хук хаба внешнего Codex/Cursor
            ext_dir=args.ext_dir,
            ext_pid=args.ext_pid,
        )
        raise SystemExit(code)
    if args.sub_command == "wait":
        from .mcp_server.setup import hub_executable
        from .sub_wait import EXIT_DENIED, NO_RESTART, run_wait, wait_command

        # Ф14-5 П14: номер подписки на проект ИЛИ `--channels` (подписки сессии на беседы) — ровно одно.
        if (args.sub_id is None) == (not args.channels):
            print("Хаб: нужен номер подписки на проект или --channels (что-то одно) — ожидание невозможно.")
            print(NO_RESTART)
            raise SystemExit(EXIT_DENIED)
        profile = args.profile
        command = wait_command(
            hub_executable(), args.sub_id, profile or "?", args.url, channels=args.channels
        )
        code = run_wait(
            args.sub_id,
            channels=args.channels,
            profile=profile,
            url=args.url,
            config_dir=paths.config_dir(),
            command=command,
            out=sys.stdout,
            err=sys.stderr,
            oc_session=args.oc_session,
            oc_dir=args.oc_dir,
            ext_session=args.ext_session,  # Ф74-2: хук хаба внешнего Codex/Cursor
            ext_dir=args.ext_dir,
            ext_pid=args.ext_pid,
        )
        raise SystemExit(code)
    with _admin_client() as client:
        if args.sub_command == "free":
            # Ф35-1 Н3: общий выключатель агентов вне проектов.
            r = client.post("/admin/subs/free", json={"enabled": args.state == "on"})
            r.raise_for_status()
            print(json.dumps(r.json(), ensure_ascii=False))
            return
        if args.sub_command == "list":
            # Ф92-1 (B5): `--days N` — завершённые за N суток (у них `end_reason`); по умолчанию сутки.
            params = {"all": 1, **({"days": args.days} if args.days != 1 else {})}
            r = client.get("/admin/subs", params=params)
            r.raise_for_status()
            body = r.json()
            for row in body.get("items") or []:
                # Ф35-1: номер роли (`role_no`) и режим (`mode`: `direct` — только адресованное агенту) — в
                # строке ядра; подписка агента вне проектов (проект 0) подписана словами.
                if row.get("project_id") == 0:
                    row = {**row, "project": "вне проектов"}
                print(json.dumps(row, ensure_ascii=False))
            # Ф14-5 C5: подписки на беседы — id с префиксом `c`.
            for row in body.get("channels") or []:
                print(json.dumps({**row, "id": f"c{row.get('id')}"}, ensure_ascii=False))
            return
        raw = args.sub_id.strip()
        is_channel = raw[:1] in ("c", "C")
        number = raw[1:] if is_channel else raw
        if not number.isdigit():
            raise SystemExit(f"hub sub end: номер подписки — число или c<число>, а не {raw!r}")
        path = f"/admin/chan-subs/{int(number)}/end" if is_channel else f"/admin/subs/{int(number)}/end"
        r = client.post(path)
        if r.status_code == 404:
            raise SystemExit(f"hub sub end: подписки {raw} нет")
        r.raise_for_status()
        row = r.json().get("sub") or {}
        if is_channel:
            row = {**row, "id": f"c{row.get('id')}"}
        print(json.dumps(row, ensure_ascii=False))


def _cmd_token_create(args: argparse.Namespace) -> None:
    scopes = args.scopes.split(",") if args.scopes else []
    project_ids = [int(x) for x in args.project_ids.split(",")] if args.project_ids else None
    with _admin_client() as client:
        r = client.post(
            "/admin/tokens",
            json={
                "name": args.name,
                "kind": args.kind,
                "scopes": scopes,
                "project_ids": project_ids,
                "ttl_days": args.ttl_days,
            },
        )
        r.raise_for_status()
        print(r.json()["token"])


def _cmd_human_agent_token(args: argparse.Namespace) -> None:
    """Ф54-1 П3: токен ответчика заявок «нужен человек» — отдельный, ровно со скоупом `human` (ни чтения
    лент, ни черновиков); `--project` ограничивает проекты. Значение печатается один раз; отзыв —
    `hub token revoke <id>` (номер — строкой в stderr и в `hub token list`)."""
    project_ids = sorted(set(args.project)) if args.project else None
    with _admin_client() as client:
        r = client.post(
            "/admin/tokens",
            json={
                "name": args.name,
                "kind": "background",
                "scopes": ["human"],
                "project_ids": project_ids,
                "ttl_days": args.ttl_days,
            },
        )
        if r.status_code == 422:
            raise SystemExit(f"hub human-agent token: {r.json().get('detail')}")
        r.raise_for_status()
        body = r.json()
        where = "все проекты" if project_ids is None else "проекты " + ", ".join(map(str, project_ids))
        print(f"токен ответчика #{body['token_id']} ({where}); отзыв: hub token revoke {body['token_id']}",
              file=sys.stderr)  # fmt: skip
        print(body["token"])


def _cmd_term_agent(args: argparse.Namespace) -> None:
    """Ф56-5 (2.4): ключ агента владельца к терминалу — `token [--ttl-days N]`, `key`, `revoke`."""
    from .term import agent_key

    config_dir = paths.config_dir()
    cmd = args.term_agent_command
    if cmd == "key":
        current = agent_key.read_key(config_dir)
        if current is None:
            print(agent_key.NO_KEY_TEXT, file=sys.stderr)
            raise SystemExit(agent_key.EXIT_NO_KEY)
        print(current[1])
        return
    with _admin_client() as client:
        try:
            if cmd == "token":
                text = agent_key.issue(client, config_dir, ttl_days=args.ttl_days)
            else:
                text = agent_key.revoke(client, config_dir)
        except agent_key.AgentKeyError as exc:
            raise SystemExit(f"hub term-agent {cmd}: {exc}") from exc
    print(text)


def _cmd_digest(args: argparse.Namespace) -> None:
    """Ф86-1 (C1.3, C1.6): сводка дня — `token [--ttl-days N] [--print]`, `revoke`, `show (--project N |
    --cwd PATH) [--day ГГГГ-ММ-ДД] [--max N]`. `show` печатает готовый блок `<hub_digest>` (для хука ручных
    окон); сводки нет, проект не нашёлся или ядро недоступно — пустой вывод и код 0 (хук не шумит)."""
    from . import digest_key

    config_dir = paths.config_dir()
    cmd = args.digest_command
    if cmd == "show":
        params: dict = {"max": args.max}
        if args.project is not None:
            params["project_id"] = args.project
        else:
            params["cwd"] = os.path.abspath(os.path.expanduser(args.cwd))
        if args.day:
            params["day"] = args.day
        try:
            with _admin_client(timeout=10.0) as client:
                r = client.get("/admin/digest", params=params)
        except (SystemExit, httpx.HTTPError):
            return
        if r.status_code == 200 and (block := r.json().get("block")):
            print(block)
        return
    with _admin_client() as client:
        try:
            if cmd == "token":
                text, token = digest_key.issue(client, config_dir, ttl_days=args.ttl_days)
            else:
                text, token = digest_key.revoke(client, config_dir), None
        except digest_key.DigestKeyError as exc:
            raise SystemExit(f"hub digest {cmd}: {exc}") from exc
    print(text, file=sys.stderr)
    if token is not None and args.print:
        print(token)


def _cmd_term_list(_args: argparse.Namespace) -> None:
    """Ф56-5: `hub term list` — терминалы хаба (номер, что идёт и где, размер, чей)."""
    from .term import agent_key

    with _admin_client() as client:
        r = client.get("/admin/term")
        if r.status_code == 503:
            raise SystemExit("hub term list: терминал в этом ядре не включён")
        r.raise_for_status()
    print(agent_key.format_list(r.json()))


def _cmd_token_list(_args: argparse.Namespace) -> None:
    """Спека 7a, A6: bearer-токены без хешей, по строке JSON на токен."""
    with _admin_client() as client:
        r = client.get("/admin/tokens")
        r.raise_for_status()
        for token in r.json():
            print(json.dumps(token, ensure_ascii=False))


def _cmd_token_revoke(args: argparse.Namespace) -> None:
    with _admin_client() as client:
        r = client.post(f"/admin/tokens/{args.token_id}/revoke")
        if r.status_code == 404:
            raise SystemExit(
                f"hub token revoke: bearer-токена {args.token_id} нет (UI-сессии так не отзываются)"
            )
        r.raise_for_status()
        print("ok")


# --- hub channel (спека 7a, П4: перенос канала владельцем, без ограничения «не расширять видимость») ------------


def _cmd_channel_show(args: argparse.Namespace) -> None:
    with _admin_client() as client:
        r = client.get(f"/admin/channels/{args.channel_id}")
        if r.status_code == 404:
            raise SystemExit(f"hub channel show: канала {args.channel_id} нет")
        r.raise_for_status()
        print(json.dumps(r.json(), ensure_ascii=False, indent=2))


_CHANNEL_WAIT_MARGIN_S = 15  # Ф11-1 C: HTTP-запрос живёт дольше ожидания ядра


def _cmd_channel_wait(args: argparse.Namespace) -> None:
    """Ф11-1 C: ждать новое сообщение беседы (админская ручка A3). Без `--follow` — JSON ответа и выход (код 0
    и при таймауте); с `--follow` — строка компактного JSON на каждый ответ с `woke`, курсор несётся сам."""
    params: dict = {"timeout_s": args.timeout, "skip_mine": 0 if args.include_mine else 1}
    if args.thread is not None:
        params["thread"] = args.thread
    after = args.after
    path = f"/admin/channels/{args.channel_id}/wait"
    try:
        with _admin_client(timeout=args.timeout + _CHANNEL_WAIT_MARGIN_S) as client:
            while True:
                try:
                    r = client.get(path, params=params if after is None else {**params, "after": after})
                except httpx.HTTPError as exc:
                    reason = exc.__class__.__name__
                    raise SystemExit(f"hub channel wait: ядро не ответило ({reason})") from exc
                if r.status_code == 404:
                    raise SystemExit(f"hub channel wait: канала {args.channel_id} нет")
                if r.status_code != 200:
                    raise SystemExit(f"hub channel wait: ошибка ядра (HTTP {r.status_code}): {r.text[:300]}")
                body = r.json()
                if not args.follow:
                    print(json.dumps(body, ensure_ascii=False, indent=2))
                    return
                after = body["cursor"]
                if body["woke"]:
                    print(json.dumps(body, ensure_ascii=False, separators=(",", ":")), flush=True)
    except KeyboardInterrupt:
        raise SystemExit(130) from None


def _cmd_channel_archive(args: argparse.Namespace) -> None:
    """Ф11-9c: беседы в архив (открытые карточки закрываются) или `--undo` — из архива (карточки остаются
    закрытыми). Строка JSON на беседу; беседы нет — строка с ошибкой, остальные обрабатываются."""
    with _admin_client() as client:
        for channel_id in args.channel_ids:
            r = client.patch(f"/admin/channels/{channel_id}", json={"archived": not args.undo})
            if r.status_code == 404:
                print(json.dumps({"id": channel_id, "error": "беседы нет"}, ensure_ascii=False))
                continue
            if r.status_code == 422:
                raise SystemExit(f"hub channel archive: {r.json().get('detail')}")
            r.raise_for_status()
            channel = r.json()
            print(
                json.dumps(
                    {
                        "id": channel["id"],
                        "archived": channel["archived"],
                        "closed_cards": channel.get("closed_cards", 0),
                        "title": channel["title"],
                    },
                    ensure_ascii=False,
                )
            )


def _cmd_channel_move(args: argparse.Namespace) -> None:
    if args.rules:
        body: dict = {"map_source": "auto"}
    elif args.unsorted:
        body = {"project_id": None}
    else:
        body = {"project_id": args.project}
    with _admin_client() as client:
        r = client.patch(f"/admin/channels/{args.channel_id}", json=body)
        if r.status_code in (404, 422):
            raise SystemExit(f"hub channel move: {r.json().get('detail')}")
        r.raise_for_status()
        channel = r.json()
    print(
        json.dumps(
            {
                "id": channel["id"],
                "project_id": channel["project_id"],
                "map_reason": channel["map_reason"],
                "title": channel["title"],
            },
            ensure_ascii=False,
        )
    )


def _cmd_channel_attention(args: argparse.Namespace) -> None:
    """Ф8-3 A6: ручной режим внимания беседы; `auto` — снять (режим по политике проекта)."""
    mode = None if args.mode == "auto" else args.mode
    with _admin_client() as client:
        r = client.patch(f"/admin/channels/{args.channel_id}", json={"attention": mode})
        if r.status_code in (404, 422):
            raise SystemExit(f"hub channel attention: {r.json().get('detail')}")
        r.raise_for_status()
        channel = r.json()
    print(
        json.dumps(
            {
                "id": channel["id"],
                "attention": channel["attention"],
                "attention_manual": channel["attention_manual"],
                "attention_auto": channel["attention_auto"],
            },
            ensure_ascii=False,
        )
    )


def _cmd_channel_autosend(args: argparse.Namespace) -> None:
    """Ф37-1 A3 (аварийно, по внутреннему сокету): список «без подтверждения». `hub channel autosend list` —
    весь список; `hub channel autosend ID --role РОЛЬ on|off` — включить или выключить роль у беседы."""
    with _admin_client() as client:
        if args.target == "list":
            if args.role is not None or args.state is not None:
                raise SystemExit("hub channel autosend list: без --role и on|off")
            r = client.get("/admin/autosend")
            r.raise_for_status()
            for item in r.json()["items"]:
                print(json.dumps(item, ensure_ascii=False))
            return
        if not args.target.isdigit() or args.role is None or args.state is None:
            raise SystemExit("hub channel autosend: нужно `ID --role РОЛЬ on|off` или `list`")
        r = client.put(
            f"/admin/channels/{int(args.target)}/autosend", json={"role": args.role, "on": args.state == "on"}
        )
        if r.status_code in (404, 422):
            raise SystemExit(f"hub channel autosend: {r.json().get('detail')}")
        r.raise_for_status()
        print(json.dumps(r.json(), ensure_ascii=False))


# --- hub mcp (спека 7a, раздел B: MCP-сервер на stdio и настройка профиля) ----------------------------------


def _cmd_mcp(args: argparse.Namespace) -> None:
    if args.mcp_command == "setup":
        from .mcp_server import setup as mcp_setup

        with _admin_client() as client:
            try:
                text = mcp_setup.run_setup(
                    client,
                    config_dir=paths.config_dir(),
                    profile=args.profile,
                    projects=args.projects,
                    organize=args.organize,
                    attach=args.attach,
                )
            except mcp_setup.SetupError as exc:
                raise SystemExit(f"hub mcp setup: {exc}") from exc
        print(text)
        return
    if args.mcp_command == "attach":
        from .mcp_server import setup as mcp_setup

        with _admin_client() as client:
            try:
                text = mcp_setup.run_attach(
                    client, config_dir=paths.config_dir(), profile=args.profile, on=args.state == "on"
                )
            except mcp_setup.SetupError as exc:
                raise SystemExit(f"hub mcp attach: {exc}") from exc
        print(text)
        return

    # Сам сервер: stdout — только протокол MCP; ничего не печатать. Нет профиля — ошибка в stderr до старта.
    if args.profile is None:
        raise SystemExit("hub mcp: нужен --profile NAME (настройка: hub mcp setup --profile NAME)")
    from .mcp_server import server as mcp_server

    mcp_server.run(profile=args.profile, config_dir=paths.config_dir(), base_url=args.url)


# --- hub agent (спека 7b, B2; Ф57-1: дежурный агент, ярус 1 — разборщик по правилам, без модели) ----------


def _cmd_agent(args: argparse.Namespace) -> None:
    from .agent import main as agent_main

    if args.agent_command == "setup":
        with _admin_client() as client:
            try:
                text = agent_main.run_setup(client, config_dir=paths.config_dir(), data_dir=paths.data_dir())
            except agent_main.SetupError as exc:
                raise SystemExit(f"hub agent setup: {exc}") from exc
        print(text)
        return
    if args.agent_command == "status":
        max_seq = None
        try:
            with _admin_client() as client:
                r = client.get("/admin/status")
                if r.status_code == 200:
                    max_seq = r.json().get("max_seq")
        except SystemExit, httpx.HTTPError:
            max_seq = None  # core не запущен — статус из файла состояния всё равно показываем
        print(agent_main.status_text(paths.data_dir(), max_seq=max_seq, now_ms=int(time.time() * 1000)))
        return
    code = agent_main.run(config_dir=paths.config_dir(), data_dir=paths.data_dir(), base_url=args.url)
    raise SystemExit(code)


# --- hub transcribe (Ф14-6, 14-6a, раздел E: расшифровка голосовых локальной моделью) ---------------------


def _cmd_transcribe(args: argparse.Namespace) -> None:
    from .transcribe import main as transcribe_main

    if args.transcribe_command == "setup":
        with _admin_client() as client:
            try:
                ok, text = transcribe_main.run_setup(
                    client,
                    config_dir=paths.config_dir(),
                    data_dir=paths.data_dir(),
                    engine_python=args.engine_python,
                    model_dirs=args.model_dir,
                    device=args.device,
                )
            except transcribe_main.SetupError as exc:
                raise SystemExit(f"hub transcribe setup: {exc}") from exc
        print(text)
        if not ok:
            raise SystemExit(1)
        return
    if args.transcribe_command == "status":
        queue = None
        try:
            with _admin_client() as client:
                r = client.get("/admin/watch")
                if r.status_code == 200:
                    queue = r.json().get("transcribe")
        except SystemExit, httpx.HTTPError:
            queue = None  # core не запущен — состояние службы из файла всё равно показываем
        print(transcribe_main.status_text(paths.data_dir(), queue))
        return
    code = transcribe_main.run(config_dir=paths.config_dir(), data_dir=paths.data_dir(), base_url=args.url)
    raise SystemExit(code)


# --- hub listen (Ф60-1: помощник на интервью — токен службы записи, «Подскажи» клавишей, состояние) ---------


def _cmd_listen(args: argparse.Namespace) -> None:
    from .listen import commands as listen_commands

    if args.listen_command == "setup":
        with _admin_client() as client:
            try:
                text = listen_commands.run_setup(
                    client, config_dir=paths.config_dir(), data_dir=paths.data_dir()
                )
            except listen_commands.SetupError as exc:
                raise SystemExit(f"hub listen setup: {exc}") from exc
        print(text)
        return
    if args.listen_command == "run":
        from .listen import service as listen_service

        raise SystemExit(listen_service.run(config_dir=paths.config_dir(), base_url=args.url))
    if args.listen_command == "hint":
        code, text = listen_commands.hint(config_dir=paths.config_dir(), base_url=args.url)
        print(text)
        if code:
            raise SystemExit(code)
        return
    data = None
    try:
        with _admin_client() as client:
            r = client.get("/admin/listen")
            if r.status_code == 200:
                data = r.json()
    except SystemExit, httpx.HTTPError:
        data = None  # core не запущен — токен всё равно показываем
    print(listen_commands.status_text(paths.config_dir(), data))


# --- hub notify (Ф33-2: спутник уведомлений рабочего стола) -------------------------------------------------


def _cmd_notify(args: argparse.Namespace) -> None:
    from .notify import commands as notify_commands

    if args.notify_command == "setup":
        with _admin_client() as client:
            try:
                text = notify_commands.run_setup(client, config_dir=paths.config_dir())
            except notify_commands.SetupError as exc:
                raise SystemExit(f"hub notify setup: {exc}") from exc
        print(text)
        return
    if args.notify_command == "run":
        from .notify import service as notify_service

        raise SystemExit(notify_service.run(config_dir=paths.config_dir(), base_url=args.url))
    print(notify_commands.status(paths.config_dir(), base_url=args.url))


# --- hub work (Ф9-1, 9a; с Ф41-2б — хост сессий хаба) -------------------------------------------------------


def _cmd_work(args: argparse.Namespace) -> None:
    if args.work_command == "guard":
        # Хук PreToolUse рабочей сессии: тот же лёгкий модуль, что точка входа `hub-work-guard`.
        from .work import guard

        argv = ["--workdir", args.workdir]
        if args.log is not None:
            argv += ["--log", args.log]
        guard.main(argv)
        return

    from .work import main as work_main

    if args.work_command == "setup":
        with _admin_client() as client:
            try:
                text = work_main.run_setup(
                    client, config_dir=paths.config_dir(), data_dir=paths.data_dir(), base_url=args.url
                )
            except work_main.SetupError as exc:
                raise SystemExit(f"hub work setup: {exc}") from exc
        print(text)
        return
    if args.work_command == "status":
        print(work_main.status_text(data_dir=paths.data_dir(), now_s=time.time()))
        return
    code = work_main.run(config_dir=paths.config_dir(), data_dir=paths.data_dir(), base_url=args.url)
    raise SystemExit(code)


# --- hub remote (Ф10-7: доступ с телефона — аварийный путь; всё остальное в UI) ----------------------------


def _cmd_remote(args: argparse.Namespace) -> None:
    with _admin_client() as client:
        if args.remote_command == "off":
            r = client.post("/admin/remote/off")
            r.raise_for_status()
            print(f"доступ с телефона выключен; отозвано телефонных сессий: {r.json()['revoked_remote']}")
            return
        r = client.get("/admin/remote")
        r.raise_for_status()
        print(json.dumps(r.json(), ensure_ascii=False, indent=2))


# --- hub engine (Ф16-1: движок сессий — показать или переключить; переключатель есть и в UI) ---------------


def _engine_text(body: dict) -> str:
    """Строки `hub engine` (Ф78-2): движок агента владельца и фактов, модель и усилие, когда переключён, и по
    каждому движку — доступность, выбор владельца или умолчание."""
    effort = body.get("effort")
    lines = [f"движок: {body.get('engine')} · {body.get('model')}" + (f" · {effort}" if effort else "")]
    changed = body.get("changed_ts")
    when = time.strftime("%d.%m %H:%M", time.localtime(changed / 1000)) if changed else "не переключался"
    lines.append(f"переключён: {when}")
    for item in body.get("engines") or []:
        if not isinstance(item, dict):
            continue
        model = item.get("model") or f"{item.get('default_model')} (умолчание)"
        level = item.get("effort") or item.get("default_effort")
        head = f"  {item.get('name')}: {model}" + (f" · {level}" if level else "")
        if not item.get("available"):
            head += f" — недоступен: {item.get('reason') or 'причина не указана'}"
        lines.append(head)
    facts = _facts_engine_line(body)
    if facts:
        lines.append(facts)
    lines += _fallback_lines(body)
    return "\n".join(lines)


def _choice_text(choice) -> str:
    if not isinstance(choice, dict) or not choice.get("engine"):
        return "нет"
    model, effort = choice.get("model"), choice.get("effort")
    return str(choice["engine"]) + (f" · {model}" if model else "") + (f" · {effort}" if effort else "")


def _fallback_lines(body: dict) -> list[str]:
    """Ф83-1: строки запасных агента владельца и фактов и «на запасном» (ядро без них — пусто)."""
    if "agent_fallback" not in body:
        return []
    facts = body.get("facts") if isinstance(body.get("facts"), dict) else {}
    lines = []
    for who, title, block in (("agent", "агент", body), ("facts", "факты", facts)):
        line = f"запасной ({title}): {_choice_text(block.get('fallback'))}"
        on = block.get("on_fallback")
        if isinstance(on, dict):
            since = on.get("since")
            when = time.strftime("%d.%m %H:%M", time.localtime(since / 1000)) if since else "?"
            line += f" — НА ЗАПАСНОМ с {when}: {on.get('kind')}"
            line += f" ({on.get('reason')})" if on.get("reason") else ""
            line += f"; вернуть: hub engine fallback {who} return"
        lines.append(line)
    return lines


def _facts_engine_line(body: dict) -> str | None:
    """Ф78-2 (раздел 12): строка выбора службы фактов; ядро без блока `facts` — `None`."""
    facts = body.get("facts")
    if not isinstance(facts, dict):
        return None
    effort = facts.get("effort")
    whose = "как у агента" if facts.get("follows_agent") else "своё"
    return (
        f"факты о людях: {facts.get('engine')} · {facts.get('model')}"
        + (f" · {effort}" if effort else "")
        + f" ({whose})"
    )


def _engine_post(client, path: str, payload: dict, what: str) -> dict:
    r = client.post(path, json=payload)
    if r.status_code != 200:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = None
        raise SystemExit(f"{what}: HTTP {r.status_code}" + (f" — {detail}" if detail else ""))
    return r.json()


def _cmd_engine_fallback(args: argparse.Namespace) -> None:
    """`hub engine fallback <agent|facts> [set <движок> [--model M] [--effort E] | off | return]` (Ф83-1):
    запасной движок агента владельца или фактов; `return` — «Вернуть на основной»."""
    command = getattr(args, "fallback_command", None)
    who = args.who
    path = f"/admin/engine/fallback/{who}"
    with _admin_client() as client:
        if command == "set":
            payload: dict = {"engine": args.engine}
            if args.model is not None:
                payload["model"] = args.model or None
            if args.effort is not None:
                payload["effort"] = args.effort or None
            body = _engine_post(client, path, payload, "hub engine fallback set")
        elif command == "off":
            body = _engine_post(client, path, {"engine": None}, "hub engine fallback off")
        elif command == "return":
            body = _engine_post(client, f"{path}/return", {}, "hub engine fallback return")
            if not body.get("returned"):
                print("не был на запасном")
        else:
            r = client.get("/admin/engine")
            r.raise_for_status()
            body = r.json()
    lines = _fallback_lines(body)
    print("\n".join(lines) if lines else "ядро не знает запасных движков (старая версия)")


def _cmd_engine_facts(args: argparse.Namespace) -> None:
    """`hub engine facts [set <движок> [--model M] [--effort E] | off]` (Ф78-2, раздел 12): свой выбор службы
    фактов; `off` — снова за агентом."""
    command = getattr(args, "facts_command", None)
    with _admin_client() as client:
        if command == "set":
            payload: dict = {"engine": args.engine}
            if args.model is not None:
                payload["model"] = args.model or None  # `--model ""` — умолчание движка
            if args.effort is not None:
                payload["effort"] = args.effort or None
            body = _engine_post(client, "/admin/engine/facts", payload, "hub engine facts set")
        elif command == "off":
            body = _engine_post(client, "/admin/engine/facts", {"engine": None}, "hub engine facts off")
        else:
            r = client.get("/admin/engine")
            r.raise_for_status()
            body = r.json()
    print(_facts_engine_line(body) or "ядро не знает отдельного выбора фактов (старая версия)")


def _autopilot_text(body: dict) -> str:
    shared = body.get("global") or {}
    projects = body.get("projects") or {}
    free = [s for s in body.get("switches") or [] if shared.get(s["key"]) is False]
    lines = [f"выключатели автопилота: стоит {len(shared) - len(free)} из {len(shared)} (общие)"]
    for switch in free:
        lines.append(f"  снят: {switch['key']} — {switch['title']}")
    for project_id, own in sorted(projects.items()):
        parts = ", ".join(f"{key}={'стоит' if hold else 'снят'}" for key, hold in sorted(own.items()))
        lines.append(f"  проект {project_id}: {parts}")
    return "\n".join(lines)


def _cmd_autopilot(args: argparse.Namespace) -> None:
    """Ф41-5 (3.2): показать выключатели автопилота; `hold-all` — поставить все (авария). Настройка — из
    интерфейса. Ф66-1: делегата карточек нет — кто отвечает на карточки, задаёт таблица маршрута проекта
    (`hub project set N --card-route …`)."""
    command = getattr(args, "autopilot_command", None)
    with _admin_client() as client:
        if command == "hold-all":
            r = client.post("/admin/autopilot/hold-all")
        else:
            r = client.get("/admin/autopilot")
        if r.status_code != 200:
            raise SystemExit(f"hub autopilot: HTTP {r.status_code}")
        print(_autopilot_text(r.json()))


def _cmd_engine(args: argparse.Namespace) -> None:
    if getattr(args, "engine_command", None) == "mcp-export":
        # Ф74-2 (п.4): набор MCP Claude Code в формате другого движка — без ядра, из своего терминала.
        from .engine import mcp_export

        folder = os.path.realpath(args.dir) if args.dir else None
        if folder is not None and not os.path.isdir(folder):
            raise SystemExit(f"hub engine mcp-export: нет папки {args.dir}")
        out = Path(args.out) if args.out else None
        sys.stdout.write(mcp_export.export(args.format, home=Path.home(), folder=folder, out=out))
        return
    if getattr(args, "engine_command", None) == "setup":
        # Ф16-3, П8: путь к opencode, версия, каталоги node и имена MCP — из окружения владельца, без ядра.
        from .engine import setup as engine_setup

        try:
            print(engine_setup.run_setup(paths.config_dir()))
        except engine_setup.SetupError as exc:
            raise SystemExit(f"hub engine setup: {exc}") from exc
        return
    if getattr(args, "engine_command", None) == "facts":
        _cmd_engine_facts(args)
        return
    if getattr(args, "engine_command", None) == "fallback":
        _cmd_engine_fallback(args)
        return
    with _admin_client() as client:
        if getattr(args, "engine_command", None) != "set":
            r = client.get("/admin/engine")
            r.raise_for_status()
            print(_engine_text(r.json()))
            return
        payload: dict = {"engine": args.engine}
        if args.model is not None:
            payload["model"] = args.model or None  # `--model ""` — умолчание движка
        if args.effort is not None:
            payload["effort"] = args.effort or None
        r = client.post("/admin/engine", json=payload)
        if r.status_code != 200:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = None
            raise SystemExit(f"hub engine set: HTTP {r.status_code}" + (f" — {detail}" if detail else ""))
        print(_engine_text(r.json()))


# --- hub load (Ф52-1: учёт нагрузки ноутбука — показать, записать на время команды, снять) ------------------

_LOAD_TIMEOUT_S = 3.0  # обращение к ядру из `hub load run`: учёт не должен задерживать работу


def _load_detail(r: httpx.Response) -> str:
    try:
        detail = r.json().get("detail")
    except ValueError, AttributeError:
        detail = None
    return detail if isinstance(detail, str) else f"HTTP {r.status_code}"


def _load_payload(args: argparse.Namespace, pid: int) -> dict:
    """`--who` по умолчанию — имя текущей папки; `--mem-gb` — в МБ."""
    return {
        "who": args.who or Path.cwd().name or "?",
        "what": args.what,
        "cores": args.cores,
        "mem_mb": round(args.mem_gb * 1024),
        "pid": pid,
        "minutes": args.minutes,
        "host": hostid.host_id(),
    }


def _load_localize(body: dict) -> dict:
    """Ф33-6: ядра и память — этой машины, записи — этой машины (ядро может стоять на другой)."""
    from .core import load as core_load

    return load_text.localize(body, hostid.host_id(), core_load.system())


def _load_claim_quiet(payload: dict) -> int | None:
    """Записать, не мешая работе: ядро недоступно или отказало — строка в stderr и `None`."""
    try:
        with _admin_client(timeout=_LOAD_TIMEOUT_S) as client:
            r = client.post("/admin/load/claims", json=payload)
    except SystemExit, httpx.HTTPError, OSError:
        print("хаб недоступен — запуск без записи", file=sys.stderr)
        return None
    if r.status_code != 200:
        print(f"хаб не принял запись ({_load_detail(r)}) — запуск без записи", file=sys.stderr)
        return None
    try:
        return int(r.json()["id"])
    except ValueError, KeyError, TypeError:
        print("хаб недоступен — запуск без записи", file=sys.stderr)
        return None


def _load_release_quiet(claim_id: int) -> None:
    """Снять запись; не вышло (ядро перезапускается) — её снимет чистка по pid."""
    try:
        with _admin_client(timeout=_LOAD_TIMEOUT_S) as client:
            client.delete(f"/admin/load/claims/{claim_id}")
    except SystemExit, httpx.HTTPError, OSError:
        pass


def _terminal_sent_sigint() -> bool:
    """Ctrl+C в терминале: SIGINT получает вся группа переднего плана, ребёнок — тоже; повторять не надо."""
    try:
        return os.tcgetpgrp(sys.stdin.fileno()) == os.getpgrp()
    except OSError, ValueError:
        return False


LOAD_MEM_FACTOR = 1.5  # потолок памяти команды = заявленное `--mem-gb` × 1,5 (владелец 14.10)
LOAD_MEM_DEFAULT_GB = 6.0


def _load_mem_cap_gb(mem_gb: float | None) -> float:
    if not mem_gb or mem_gb <= 0:
        return LOAD_MEM_DEFAULT_GB
    return round(mem_gb * LOAD_MEM_FACTOR, 2)


def _load_scope(command: list[str], cap_gb: float) -> tuple[list[str], str | None]:
    """Обернуть команду в свою клетку systemd (`systemd-run --user --scope`) с потолком памяти: перерасход
    убивает только эту команду, процессы вне обёртки не трогаются. `systemd-run --scope` сам исполняет команду
    (pid ребёнка — pid команды, сигналы доходят как раньше). Нет systemd-run или пользовательского менеджера —
    команда без клетки, строка в stderr."""
    runner = shutil.which("systemd-run")
    systemctl = shutil.which("systemctl")
    if (
        runner is None
        or systemctl is None
        or not os.environ.get("XDG_RUNTIME_DIR")
        or not _user_manager_ok(systemctl)
    ):
        print("hub-load: systemd-run недоступен — запуск без потолка памяти", file=sys.stderr)
        return command, None
    unit = f"hub-load-{os.getpid()}-{secrets.token_hex(3)}.scope"
    wrapped = [
        runner,
        "--user",
        "--scope",
        "--quiet",
        f"--unit={unit}",
        "-p",
        f"MemoryMax={int(cap_gb * 1024)}M",
        "-p",
        "MemorySwapMax=0",
        "--",
        *command,
    ]
    return wrapped, unit


def _user_manager_ok(systemctl: str) -> bool:
    """Ревью Ф60-1 (5): пользовательский менеджер отвечает (песочница, вход по ssh — может не быть шины);
    иначе `systemd-run` выйдет с ошибкой и команда не запустится вовсе."""
    try:
        res = subprocess.run(  # noqa: S603 — фиксированная команда systemctl
            [systemctl, "--user", "show", "-p", "Version", "--value"], capture_output=True, timeout=3
        )
    except OSError, subprocess.SubprocessError:
        return False
    return res.returncode == 0


def _load_scope_result(unit: str, code: int) -> str:
    """Итог клетки (`oom-kill`, `success`, …) и уборка упавшей клетки, чтобы не копились. Убита SIGKILL
    (137) — systemd мог ещё не отметить OOM: до секунды переспрашиваем (ревью Ф60-1, 4)."""
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return ""
    try:
        for _ in range(10):
            res = subprocess.run(  # noqa: S603 — фиксированная команда systemctl
                [systemctl, "--user", "show", "-p", "Result", "--value", unit],
                capture_output=True,
                text=True,
                timeout=5,
            )
            result = res.stdout.strip()
            if result == "oom-kill" or code != 137:
                break
            time.sleep(0.1)
        subprocess.run(  # noqa: S603
            [systemctl, "--user", "reset-failed", unit], capture_output=True, timeout=5
        )
    except OSError, subprocess.SubprocessError:
        return ""
    return result


def _cmd_load_run(args: argparse.Namespace) -> None:
    """`hub load run … -- <команда>`: запись со СВОИМ pid на время команды. Запись снимается в `finally`
    при любом исходе; SIGINT/SIGTERM пересылаются ребёнку; код выхода — код команды (убита сигналом —
    128 + номер). Ядро недоступно — команда всё равно запускается. Команда идёт в своей клетке systemd с
    потолком памяти `--mem-gb` × 1,5 (по умолчанию 6 ГБ): перерасход убивает только её (код 137, строка
    «hub-load: команда убита по памяти»)."""
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise SystemExit("hub load run: после `--` нужна команда")
    exe = command[0] if os.sep in command[0] else shutil.which(command[0])
    if exe is None or not os.path.exists(exe):
        print(f"hub load run: {command[0]}: команда не найдена", file=sys.stderr)
        raise SystemExit(127)
    if not os.access(exe, os.X_OK) or os.path.isdir(exe):
        print(f"hub load run: {command[0]}: нет прав на запуск", file=sys.stderr)
        raise SystemExit(126)
    cap_gb = _load_mem_cap_gb(getattr(args, "mem_gb", None))
    command, unit = _load_scope(command, cap_gb)
    child: subprocess.Popen | None = None
    early: list[int] = []  # сигналы до запуска команды

    def _forward(signum: int, _frame) -> None:
        if child is None:
            early.append(signum)
            return
        if signum == signal.SIGINT and _terminal_sent_sigint():
            return
        try:
            child.send_signal(signum)
        except OSError:
            pass

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, _forward)
    claim_id = _load_claim_quiet(_load_payload(args, os.getpid()))
    code = 1
    try:
        if early:
            code = 128 + early[0]
        else:
            try:
                child = subprocess.Popen(command)  # noqa: S603 — команда вызывающего, stdin/stdout/stderr насквозь
            except OSError as exc:
                print(f"hub load run: {command[0]}: {exc.strerror or exc}", file=sys.stderr)
                code = 126 if isinstance(exc, PermissionError) else 127
            else:
                for signum in list(early):  # сигнал пришёл, пока запускали
                    _forward(signum, None)
                code = child.wait()
                if code < 0:
                    code = 128 - code
                if unit is not None and _load_scope_result(unit, code) == "oom-kill":
                    print(f"hub-load: команда убита по памяти (предел {cap_gb:g} ГБ)", file=sys.stderr)
                    code = 137
    finally:
        if claim_id is not None:
            _load_release_quiet(claim_id)
    raise SystemExit(code)


def _machine_line(row: dict) -> str:
    """Ф77-1: строка `hub machine list` — показ, хеш, `hostname`, путь, версия, последний контакт."""
    parts = [str(row.get("display") or "?"), str(row.get("host") or "—")]
    if row.get("hostname") and row.get("hostname") != row.get("display"):
        parts.append(f"hostname {row['hostname']}")
    if row.get("core"):
        parts.append("машина ядра")
    elif row.get("route"):
        parts.append(f"путь {row['route']}")
    if row.get("version"):
        parts.append(f"версия {row['version']}")
    if row.get("last_ts"):
        when = time.strftime("%d.%m %H:%M", time.localtime(row["last_ts"] / 1000))
        parts.append(f"контакт {when}" + ("" if row.get("online") else " (не на связи)"))
    return " · ".join(parts)


def _cmd_machine(args: argparse.Namespace) -> None:
    """Ф77-1 (A5): реестр машин — `hub machine list`, `hub machine name <host|hostname> <имя>` (пустое имя —
    снять)."""
    with _admin_client() as client:
        r = client.get("/admin/machines")
        r.raise_for_status()
        items = [row for row in r.json().get("items") or [] if isinstance(row, dict)]
        if args.machine_command != "name":
            if getattr(args, "json", False):
                print(json.dumps(r.json(), ensure_ascii=False))
                return
            for row in items:
                print(_machine_line(row))
            if not items:
                print("машин нет")
            return
        key = args.machine.strip()
        found = [row for row in items if row.get("host") == key]
        if not found:
            found = [row for row in items if row.get("hostname") == key or row.get("name") == key]
        if not found:
            raise SystemExit(f"hub machine name: машины {key!r} нет (hub machine list)")
        if len(found) > 1:
            hosts = ", ".join(str(row.get("host")) for row in found)
            raise SystemExit(f"hub machine name: под {key!r} несколько машин — укажи хеш: {hosts}")
        r = client.patch(f"/admin/machines/{found[0]['host']}", json={"name": args.name})
        if r.status_code in (404, 422):
            raise SystemExit(f"hub machine name: {r.json().get('detail')}")
        r.raise_for_status()
        print(_machine_line(r.json().get("machine") or {}))


def _parse_around(text: str) -> int:
    """`--around "2026-10-25 12:29"` (местное время машины; допустимы секунды и `T`) -> миллисекунды."""
    from datetime import datetime

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return int(datetime.strptime(text.strip(), fmt).timestamp() * 1000)
        except ValueError:
            continue
    raise SystemExit(f"hub trace: время «{text}» не разобрано — нужно «ГГГГ-ММ-ДД ЧЧ:ММ»")


def _cmd_trace(args: argparse.Namespace) -> None:
    """Ф97-1: только чтение — полная переписка по карточке или по каналу владельца за окно."""
    if (args.ref is None) == (args.around is None):
        raise SystemExit("hub trace: нужен ровно один из вариантов — <ref> или --around «ГГГГ-ММ-ДД ЧЧ:ММ»")
    if args.ref is not None:
        params: dict = {"ref": args.ref}
    else:
        params = {"around_ms": _parse_around(args.around), "min": args.min}
        if args.project is not None:
            params["project_id"] = args.project
    with _admin_client() as client:
        r = client.get("/admin/trace", params=params)
    if r.status_code == 404:
        raise SystemExit(f"hub trace: по {args.ref} ничего нет")
    if r.status_code != 200:
        raise SystemExit(f"hub trace: {_load_detail(r)}")
    body = r.json()
    if args.json:
        print(json.dumps(body, ensure_ascii=False, indent=2))
    else:
        print(trace_text.render(body))


def _cmd_load(args: argparse.Namespace) -> None:
    command = getattr(args, "load_command", None)
    if command == "run":
        _cmd_load_run(args)
        return
    with _admin_client() as client:
        if command == "add":
            r = client.post("/admin/load/claims", json=_load_payload(args, args.pid))
            if r.status_code != 200:
                raise SystemExit(f"hub load add: {_load_detail(r)}")
            print(r.json()["id"])
            return
        if command == "done":
            r = client.delete(f"/admin/load/claims/{args.claim_id}")
            if r.status_code != 200:
                raise SystemExit(f"hub load done: {_load_detail(r)}")
            print("ok")
            return
        r = client.get("/admin/load")
        if r.status_code != 200:
            raise SystemExit(f"hub load: {_load_detail(r)}")
        body = _load_localize(r.json())
    if getattr(args, "json", False):
        print(json.dumps(body, ensure_ascii=False, indent=2))
    else:
        print(load_text.render(body))


def _cmd_status(_args: argparse.Namespace) -> None:
    with _admin_client() as client:
        r = client.get("/admin/status")
        r.raise_for_status()
        print(json.dumps(r.json(), ensure_ascii=False, indent=2))


def _cmd_account_resume(args: argparse.Namespace) -> None:
    with _admin_client() as client:
        r = client.post(f"/admin/accounts/{args.account_id}/resume")
        r.raise_for_status()
        print("ok")


def _parse_set_pairs(pairs: list[str]) -> dict:
    """`k=v`: значение — JSON (`true`, `3`, `["a@b"]`, `null` — удалить ключ), иначе строка как есть."""
    changes: dict = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not key:
            raise SystemExit(f"hub account set: ожидается ключ=значение, получено {pair!r}")
        try:
            changes[key] = json.loads(raw)
        except json.JSONDecodeError:
            changes[key] = raw
    return changes


def _remove_account_files(account_id: int, source: str) -> list[str]:
    """Файлы пустого аккаунта, которые успел завести `hub auth …`: секрет коннектора, креды источника и
    (Telegram) каталог неоконченной сессии. Только точные пути по номеру аккаунта, симлинки не трогаются."""
    connectors = paths.config_dir() / "connectors"
    removed: list[str] = []
    for path in (connectors / f"{account_id}.key", connectors / f"{account_id}.{source}.json"):
        if path.is_file() and not path.is_symlink():
            path.unlink()
            removed.append(str(path))
    session_dir = paths.data_dir() / "telegram" / str(account_id)
    if source == "telegram" and session_dir.is_dir() and not session_dir.is_symlink():
        shutil.rmtree(session_dir)
        removed.append(str(session_dir))
    return removed


def _cmd_account_rm(args: argparse.Namespace) -> None:
    """`hub account rm ID`: убрать пустой аккаунт — заведённый, но ни разу не подключившийся (вход прервали).
    Ядро отказывает, если аккаунт подключался или у него есть данные."""
    with _admin_client() as client:
        r = client.delete(f"/admin/accounts/{args.account_id}")
        if r.status_code == 404:
            raise SystemExit(f"hub account rm: аккаунта {args.account_id} нет")
        _exit_on_client_error(r, "hub account rm")
        r.raise_for_status()
        source = r.json()["source"]
    print(f"аккаунт {args.account_id} ({source}) удалён")
    for path in _remove_account_files(args.account_id, source):
        print(f"убрано: {path}")


def _cmd_account_set(args: argparse.Namespace) -> None:
    changes = _parse_set_pairs(args.pairs)
    with _admin_client() as client:
        r = client.patch(f"/admin/accounts/{args.account_id}/config", json={"set": changes})
        if r.status_code == 404:
            raise SystemExit(f"hub account set: аккаунта {args.account_id} нет")
        if r.status_code == 422:
            raise SystemExit(f"hub account set: {r.json().get('detail')}")
        r.raise_for_status()
    print(json.dumps(r.json()["config"], ensure_ascii=False, indent=2))
    print(f"действует после перезапуска: systemctl --user restart hub-connector@{args.account_id}")


# --- hub map / hub folders (раздел B спеки куска 1a: раскладка по проектам) --------------------------------


def _cmd_map_list(args: argparse.Namespace) -> None:
    params = {"account_id": args.account} if args.account is not None else {}
    with _admin_client() as client:
        r = client.get("/admin/map-rules", params=params)
        r.raise_for_status()
        for rule in r.json():
            print(json.dumps(rule, ensure_ascii=False))


def _cmd_rules_list(args: argparse.Namespace) -> None:
    """Ф63-1: правила проекта для входящих — только чтение (заводят сессия проекта и владелец в интерфейсе)."""
    with _admin_client() as client:
        r = client.get("/admin/inbound-rules", params={"project_id": args.project})
        if r.status_code == 404:
            raise SystemExit(f"hub rules list: проекта {args.project} нет")
        r.raise_for_status()
        rules = r.json()
    if not rules:
        print("правил нет")
    for rule in rules:
        state = "вкл" if rule["enabled"] else "выкл"
        who = f"роль «{rule['created_by_role']}»" if rule.get("created_by_role") else "владелец"
        idle = [w["role"] for w in rule.get("wake_status") or [] if w.get("listening") != "messages"]
        tail = f" · не слушают сообщения: {', '.join(idle)}" if idle else ""
        print(
            f"#{rule['id']} [{state}] {rule['title']} — {rule['description']} · завёл {who} · "
            f"сработало {rule['hits']}{tail}"
        )


def _cmd_rules_add(args: argparse.Namespace) -> None:
    """Правило входящих от владельца (админский сокет): поля JSON, как у ручки интерфейса, плюс `archive`."""
    try:
        fields = json.loads(args.json)
    except ValueError as exc:
        raise SystemExit(f"hub rules add: --json не JSON ({exc})") from exc
    with _admin_client() as client:
        r = client.post("/admin/inbound-rules", params={"project_id": args.project}, json=fields)
        if r.status_code >= 400:
            raise SystemExit(f"hub rules add: {r.json().get('detail')}")
        rule = r.json()
    print(f"#{rule['id']} {rule['title']} — {rule['description']}")


def _cmd_rules_rm(args: argparse.Namespace) -> None:
    with _admin_client() as client:
        r = client.delete(f"/admin/inbound-rules/{args.rule_id}", params={"project_id": args.project})
        if r.status_code >= 400:
            raise SystemExit(f"hub rules rm: {r.json().get('detail')}")
    print(f"правило #{args.rule_id} снято")


def _cmd_map_rm(args: argparse.Namespace) -> None:
    with _admin_client() as client:
        r = client.delete(f"/admin/map-rules/{args.rule_id}")
        r.raise_for_status()
        print(json.dumps(r.json(), ensure_ascii=False))


def _cmd_folders(args: argparse.Namespace) -> None:
    with _admin_client() as client:
        r = client.get(f"/admin/accounts/{args.account}/meta/tg_folders")
        if r.status_code == 404:
            print("папок нет (account_meta.tg_folders ещё не пришёл от коннектора)")
            return
        r.raise_for_status()
        for folder in r.json()["value"]:
            print(f"{folder['id']}\t{folder['title']}")


def _cmd_map_add(args: argparse.Namespace) -> None:
    """Ф14-1 A5 (П1): CLI шлёт вид и значение, условие собирает ядро (`mapping.build_match` — тот же путь,
    что у UI); проверки папок, меток и доменов — там же, отказ ядра печатается текстом."""
    if args.all:
        kind, value = "all", None
    elif args.folder is not None:
        kind, value = "folder", args.folder
    elif args.label is not None:
        kind, value = "label", args.label
    elif args.from_domain is not None:
        kind, value = "domain", args.from_domain
    else:
        kind, value = "channel", args.channel
    body = {
        "account_id": args.account,
        "project_id": args.project,
        "kind": kind,
        "value": value,
        "priority": args.priority,
    }
    with _admin_client() as client:
        r = client.post("/admin/map-rules", json=body)
    _exit_on_client_error(r, "hub map add")
    r.raise_for_status()
    print(json.dumps(r.json(), ensure_ascii=False))


# --- hub connector (раздел D спеки куска 1a: внешний цикл ConnectorBase.run()) ------------------------------

_CONNECTOR_REGISTRY = (
    "fake",
    "telegram",
    "mail",
)


def _cmd_feed_push(args: argparse.Namespace) -> None:
    from . import feed

    code = feed.run_push(args)
    if code != 0:
        raise SystemExit(code)


_CONNECTOR_WAIT_STEPS_S = (1.0, 2.0, 5.0, 10.0, 30.0)
_CONNECTOR_WAIT_LOG_EVERY_S = 60.0


async def _connector_wait_account(client, account_id, *, steps=None, log_every_s=None):
    """Старт коннектора: `GET /account` с повтором, пока ядро не поднялось (сеть, таймаут, 5xx): 1→2→5→10 с,
    потолок 30 с, без предела по времени; первая неудача и далее раз в минуту — одна строка в журнал.
    401 — число 78 (секрет отозван, юнит не перезапускает). SIGTERM/SIGINT во время ожидания — `None`
    (выйти сразу, код 0)."""
    import asyncio

    steps = steps or _CONNECTOR_WAIT_STEPS_S
    log_every_s = _CONNECTOR_WAIT_LOG_EVERY_S if log_every_s is None else log_every_s
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover
            pass
    attempt = 0
    last_log: float | None = None
    while True:
        try:
            return await client.get_account()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                print("hub connector: секрет отозван (401) — нужен новый секрет коннектора", file=sys.stderr)
                return 78
            reason = f"HTTP {exc.response.status_code}"
        except httpx.HTTPError as exc:
            reason = exc.__class__.__name__
        now = time.monotonic()
        if last_log is None or now - last_log >= log_every_s:
            last_log = now
            print(
                f"hub connector {account_id}: ядро не отвечает на GET /account ({reason}) — жду и повторяю",
                file=sys.stderr,
                flush=True,
            )
        pause = steps[min(attempt, len(steps) - 1)]
        attempt += 1
        try:
            await asyncio.wait_for(stop.wait(), timeout=pause)
        except TimeoutError:
            continue
        return None


def _cmd_connector(args: argparse.Namespace) -> None:
    import asyncio

    from .connectors.base import CoreClient

    account_id = args.account_id
    key_path = paths.config_dir() / "connectors" / f"{account_id}.key"
    if not key_path.exists():
        print(f"hub connector: {key_path} не найден — заведите аккаунт (`hub account add`)", file=sys.stderr)
        raise SystemExit(78)
    secret = key_path.read_text(encoding="utf-8").strip()
    socket_path = paths.data_dir() / _SOCKET_NAME

    async def _run() -> int:
        client = CoreClient(socket_path=str(socket_path), secret=secret)
        account = await _connector_wait_account(client, account_id)
        if account is None:  # SIGTERM/SIGINT во время ожидания ядра
            await client.aclose()
            return 0
        if account == 78:
            await client.aclose()
            return 78

        source = account["source"]
        if source == "fake":
            from .connectors.fake import FakeConnector, FakeSource

            connector = FakeConnector(client, FakeSource())
        elif source == "telegram":
            from .connectors.telegram.connector import (
                ProxyRequired,
                TelegramConnector,
                parse_proxy,
                phone_daily_of,
            )
            from .connectors.telegram.gateway import TgGateway

            config = account.get("config") or {}
            try:
                proxy = parse_proxy(config)
            except ProxyRequired as exc:
                print(f"hub connector: {exc}", file=sys.stderr)
                await client.aclose()
                return 78

            creds_path = paths.config_dir() / "connectors" / f"{account_id}.telegram.json"
            if not creds_path.exists():
                print(
                    f"hub connector: {creds_path} не найден — выполните `hub auth tg --account {account_id}`",
                    file=sys.stderr,
                )
                await client.aclose()
                return 78
            creds = json.loads(creds_path.read_text(encoding="utf-8"))

            account_dir = paths.data_dir() / "telegram" / str(account_id)
            session_path = str(account_dir / "tg")
            gateway = TgGateway(
                session_path=session_path,
                api_id=creds["api_id"],
                api_hash=creds["api_hash"],
                proxy=proxy,
                device_model=config.get("device_model", "hub"),
                system_version=config.get("system_version", "linux"),
                app_version=config.get("app_version", "0.1"),
            )
            # Раздел «Алерт о новой сессии Telegram» (кусок 1c): known-хеши сессий — тот же каталог, что
            # tg.session, 0700/0600.
            connector = TelegramConnector(
                client,
                gateway,
                account_id=account_id,
                state_path=str(account_dir / "state.json"),
                media_tmp_dir=_media_tmp_dir(account_id),
                views_refresh=True,  # Ф23-3: часовой проход просмотров постов каналов
                phone_daily=phone_daily_of(config),  # Ф34-3 П3а: `tg_phone_daily`
            )
        elif source == "mail":
            from .connectors.mail.connector import MailConnector, parse_mail_config
            from .connectors.mail.gateway import GmailGateway

            config = account.get("config") or {}
            # Ф12-4: `flavor=imap` — обычный IMAP/SMTP (`PlainMailConnector`), иначе Gmail как раньше
            plain = config.get("flavor") == "imap"
            try:
                if plain:
                    from .connectors.mail.plain import parse_plain_config

                    mail_cfg = parse_plain_config(config)
                else:
                    mail_cfg = parse_mail_config(config)
            except ValueError as exc:
                print(f"hub connector: {exc}", file=sys.stderr)
                await client.aclose()
                return 78
            creds_path = paths.config_dir() / "connectors" / f"{account_id}.mail.json"
            if not creds_path.exists():
                print(
                    f"hub connector: {creds_path} не найден — выполните "
                    f"`hub auth mail --account {account_id} --address {mail_cfg['address']}`",
                    file=sys.stderr,
                )
                await client.aclose()
                return 78
            creds = json.loads(creds_path.read_text(encoding="utf-8"))
            if plain:
                from .connectors.mail.plain import PlainMailConnector

                gateway = GmailGateway(
                    address=mail_cfg["address"],
                    password=creds["password"],
                    proxy=mail_cfg["proxy"],
                    flavor="imap",
                    imap_host=mail_cfg["imap_host"],
                    imap_port=mail_cfg["imap_port"],
                    smtp_host=mail_cfg["smtp_host"],
                    smtp_port=mail_cfg["smtp_port"],
                )
                connector = PlainMailConnector(
                    client,
                    gateway,
                    account_id=account_id,
                    config=config,
                    state_path=paths.data_dir() / "mail" / str(account_id) / "state.json",
                    media_tmp_dir=_media_tmp_dir(account_id),
                )
            else:
                gateway = GmailGateway(
                    address=mail_cfg["address"], password=creds["password"], proxy=mail_cfg["proxy"]
                )
                connector = MailConnector(
                    client,
                    gateway,
                    account_id=account_id,
                    config=config,
                    media_tmp_dir=_media_tmp_dir(account_id),
                )
        else:
            print(
                f"hub connector: неизвестный source {source!r} (реестр: {_CONNECTOR_REGISTRY})",
                file=sys.stderr,
            )
            await client.aclose()
            return 78

        # Ф32-2 п.5а: рестарт коннектора без рестарта ядра — осиротевшие `*.part` своего каталога.
        from .connectors.media import cleanup_parts

        cleanup_parts(_media_tmp_dir(account_id))
        return await connector.run(source=source, account_id=account_id)

    _setup_connector_logging()
    raise SystemExit(asyncio.run(_run()))


# Ф11-5, К2: без настройки процесс коннектора отдавал в journald только warning+ (last resort) — info-строки
# каркаса и коннекторов (итоги медиа, прочитанности, реакций; только id и классы ошибок) терялись.
# Сторонние библиотеки — на warning: Telethon и httpx пишут info на каждое подключение и запрос.
_CONNECTOR_NOISY_LOGGERS = ("httpx", "httpcore", "telethon", "websockets", "aiosmtplib", "asyncio")


def _setup_connector_logging() -> None:
    import logging

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in _CONNECTOR_NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _media_tmp_dir(account_id: int) -> Path:
    """Ф8-5 П4: временные файлы загрузчика медиа коннектора — `<data_dir>/media/.tmp-conn/<account_id>/`
    (ядро чистит `.tmp-conn` на старте)."""
    return paths.data_dir() / "media" / ".tmp-conn" / str(account_id)


# Раздел E спеки куска 1a: `/admin/backup` может ждать до 30 мин конца backfill коннектора, ПОТОМ ещё
# VACUUM INTO большой базы + age — явно больше 45 мин, с запасом. Тот же порог — `TimeoutStartSec`
# в deploy/systemd/hub-backup.service.
_BACKUP_CLIENT_TIMEOUT_S = 3000.0


# Ф33-3: снимок для restic — VACUUM INTO двух БД и копии сессий в память; минуты, не полчаса (догон не ждёт).
_SNAPSHOT_CLIENT_TIMEOUT_S = 1800.0


def _cmd_backup_snapshot(target: str) -> None:
    """Ф33-3 (К5): `hub backup --snapshot-dir /dev/shm/hub-restic-<pid>` — снимок делает ядро (единственный
    писатель БД); каталог создаёт и удаляет вызывающий (`scripts/hub-offsite-backup.sh`)."""
    path = os.path.abspath(target)
    try:
        with _admin_client(timeout=_SNAPSHOT_CLIENT_TIMEOUT_S) as client:
            r = client.post("/admin/snapshot", json={"dir": path})
    except httpx.HTTPError as exc:
        print(f"снимок не удался: {exc.__class__.__name__}", file=sys.stderr)
        raise SystemExit(1) from exc
    if r.status_code != 200:
        print(f"снимок не удался: HTTP {r.status_code} {r.text}", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(r.json(), ensure_ascii=False))


def _cmd_backup_hold(target: str) -> None:
    """Ф33-3 (К12): `hub backup --hold-outbox <каталог данных>` — строки очереди, бывшие в пути, -> «могло
    уйти» (`unknown`) до решения владельца; нажатия кнопок — `unknown`, одобренные операции над группами — снова
    карточкой (`core.restore`). Зовёт `scripts/hub-restore.sh` до того, как копия встанет на место."""
    from .core import restore  # лениво: модуль CLI без sqlite3 на старте

    try:
        held = restore.hold_outbox(Path(target).resolve(), now_ms=int(time.time() * 1000))
    except restore.RestoreError as exc:
        raise SystemExit(f"hub backup --hold-outbox: {exc}") from exc
    print(json.dumps({"ok": True, **held}, ensure_ascii=False))


def _cmd_backup(args: argparse.Namespace) -> None:
    snapshot_dir = getattr(args, "snapshot_dir", None)
    hold_outbox = getattr(args, "hold_outbox", None)
    if snapshot_dir is not None:
        _cmd_backup_snapshot(snapshot_dir)
        return
    if hold_outbox is not None:
        _cmd_backup_hold(hold_outbox)
        return
    if args.set_recipient is not None:
        try:
            backup_recipient.write_recipient(paths.config_dir(), args.set_recipient)
        except ValueError as exc:
            raise SystemExit(f"hub backup: {exc}") from exc
        print(f"recipient записан в {paths.config_dir() / backup_recipient.RECIPIENT_FILE_NAME}")
        return

    try:
        with _admin_client(timeout=_BACKUP_CLIENT_TIMEOUT_S) as client:
            r = client.post("/admin/backup")
    except httpx.HTTPError as exc:
        print(f"бэкап не удался: {exc.__class__.__name__}", file=sys.stderr)
        raise SystemExit(1) from exc

    if r.status_code != 200:
        print(f"бэкап не удался: HTTP {r.status_code} {r.text}", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(r.json(), ensure_ascii=False))


def _cmd_watch(args: argparse.Namespace) -> None:
    from . import watch as watch_mod  # лениво, тот же приём, что и с runner/backup

    admin_key_path = paths.config_dir() / _ADMIN_KEY_NAME
    admin_secret = admin_key_path.read_text(encoding="utf-8").strip() if admin_key_path.exists() else ""
    now_ms = int(time.time() * 1000)
    code = watch_mod.run_watch(
        port=args.port,
        data_dir=paths.data_dir(),
        config_dir=paths.config_dir(),
        admin_secret=admin_secret,
        now_ms=now_ms,
    )
    raise SystemExit(code)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hub", description="Личный мультимессенджер по проектам")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    core_p = sub.add_parser("core", help="поднять оба сервера (публичный + внутренний сокет)")
    core_p.add_argument(
        "--port", type=int, default=_PUBLIC_PORT, help="порт публичного приложения (для тестов)"
    )
    core_p.add_argument(
        "--remote-port",
        type=int,
        default=0,
        help="удалённый слушатель 127.0.0.1:PORT для туннеля с телефона (0 — не слушать; юнит — 8790)",
    )
    core_p.add_argument(
        "--ui-dir",
        type=Path,
        default=None,
        help="каталог собранного UI (по умолчанию <репозиторий>/ui/dist, если он существует)",
    )
    core_p.set_defaults(func=_cmd_core)

    ui_p = sub.add_parser("ui", help="открыть UI (или напечатать его URL)")
    ui_p.add_argument("--print-url", action="store_true", help="напечатать URL вместо запуска браузера")
    ui_p.set_defaults(func=_cmd_ui)

    auth_p = sub.add_parser("auth", help="ввести секрет источника (`tg` — фаза 1, остальные позже)")
    auth_p.add_argument("source")
    auth_p.add_argument("--account", type=int, default=None, help="существующий account_id (`hub auth tg`)")
    auth_p.add_argument("--title", default=None, help="заголовок нового аккаунта (`hub auth tg`)")
    auth_p.add_argument(
        "--new",
        action="store_true",
        help="завести ещё один аккаунт, даже если такой источник уже есть (`hub auth tg`)",
    )
    auth_p.add_argument(
        "--proxy", default=None, help="socks5://host:port или 'none' (`hub auth tg`/`slack`/`mail`)"
    )
    auth_p.add_argument(
        "--receive-only",
        action="store_true",
        help="почта только на приём: без проверки SMTP, ответ из хаба выключен (`hub auth mail`)",
    )
    auth_p.add_argument("--address", default=None, help="адрес ящика (`hub auth mail`)")
    auth_p.add_argument(
        "--db", choices=("main", "work"), default=None, help="файл БД нового аккаунта (`hub auth mail`)"
    )
    # Ф12-4: обычный ящик IMAP/SMTP (не Gmail) — оба ключа вместе, SSL-порты 993/465 по умолчанию
    auth_p.add_argument("--imap", default=None, help="IMAP-сервер host:port обычного ящика (`hub auth mail`)")
    auth_p.add_argument("--smtp", default=None, help="SMTP-сервер host:port обычного ящика (`hub auth mail`)")
    auth_p.add_argument("--name", default=None, help="имя отправителя обычного ящика (`hub auth mail`)")
    auth_p.add_argument(
        "--from-env-file", default=None, help="ключ и каталог из dotenv-файла (`hub auth speechkit`)"
    )
    auth_p.add_argument(
        "--force", action="store_true", help="заменить сохранённый ключ без вопроса (`hub auth speechkit`)"
    )
    auth_p.add_argument(
        "--check",
        action="store_true",
        help="пробный запрос с сохранённым ключом (`hub auth speechkit`/`alibaba`)",
    )
    auth_p.add_argument(
        "--var",
        default=None,
        help="имя переменной ключа в dotenv-файле (`hub auth alibaba`, по умолчанию DASHSCOPE_API_KEY)",
    )
    auth_p.set_defaults(func=_cmd_auth)

    account_p = sub.add_parser("account", help="управление аккаунтами источников")
    account_sub = account_p.add_subparsers(dest="account_command", required=True)
    account_add_p = account_sub.add_parser("add")
    account_add_p.add_argument("--source", required=True)
    account_add_p.add_argument("--title", default=None)
    account_add_p.add_argument("--db", choices=("main", "work"), required=True)
    account_add_p.add_argument("--config", action="append", metavar="k=v", default=None)
    account_add_p.set_defaults(func=_cmd_account_add)

    project_p = sub.add_parser("project", help="управление проектами")
    project_sub = project_p.add_subparsers(dest="project_command", required=True)
    project_add_p = project_sub.add_parser("add")
    project_add_p.add_argument("--name", required=True)
    project_add_p.add_argument("--color", default=None)
    project_add_p.add_argument("--workdir", default=None)
    project_add_p.set_defaults(func=_cmd_project_add)
    project_set_p = project_sub.add_parser("set", help="рабочая папка проекта и политика внимания")
    project_set_p.add_argument("project_id", type=int)
    workdir_group = project_set_p.add_mutually_exclusive_group()
    workdir_group.add_argument("--workdir", default=None, metavar="PATH", help="абсолютный путь к папке")
    workdir_group.add_argument("--no-workdir", action="store_true", help="снять рабочую папку")
    project_set_p.add_argument(
        "--attention",
        nargs="+",
        default=None,
        metavar="ВИД=РЕЖИМ",
        help="режим по виду беседы: dm|group|channel|topic|mail_thread|issue|card|task|mpim|default = "
        "react|info; ВИД=null — удалить",
    )
    project_set_p.add_argument(
        "--subs",
        choices=("on", "off"),
        default=None,
        help="подписки сессий Claude Code на проект (off завершает живые подписки)",
    )
    project_set_p.add_argument(
        "--session-reactions",
        choices=("on", "off"),
        default=None,
        help="реакции сессий Claude Code в беседах проекта (по правилам проекта в hub.md)",
    )
    project_set_p.add_argument(
        "--agent-msgs",
        choices=("on", "off"),
        default=None,
        help="обмен сообщениями агентов (сессий с подпиской) в проекте; по умолчанию включён",
    )
    project_set_p.add_argument(
        "--reaction-delay",
        default=None,
        metavar="A-B",
        help="пауза реакции сессии от времени сообщения, минуты (0 ≤ A ≤ B ≤ 60; по умолчанию 2-5)",
    )
    project_set_p.add_argument(
        "--session-join",
        choices=("confirm", "auto"),
        default=None,
        help="вступление сессий в группы и каналы Telegram: confirm — карточкой владельцу, auto — сразу "
        "(только подписанная сессия проекта, до 5 в день)",
    )
    project_set_p.add_argument(
        "--session-handled",
        choices=("on", "off"),
        default=None,
        help="отметка «разобрано» сессиями проекта (hub_handled): беседа у владельца прочитана и уходит из "
        "«Нужна реакция»; "
        "по умолчанию off",
    )
    project_set_p.add_argument(
        "--digest",
        choices=("on", "off"),
        default=None,
        help="сводка дня после сжатия: сессии хаба проекта получают сводку проекта за сегодня первой подачей "
        "после сжатия и в новой сессии; по умолчанию on",
    )
    project_set_p.add_argument(
        "--siblings",
        choices=("on", "off"),
        default=None,
        help="слова владельца из соседних сессий: к подаче сессии хаба — реплики владельца другим сессиям хаба "
        "проекта; по умолчанию on",
    )
    project_set_p.add_argument(
        "--agent-batch-min",
        type=int,
        default=None,
        metavar="МИН",
        help="сообщения агентов подпискам проекта — пачкой не чаще раза в МИН минут (0–60; 0 — сразу, по "
        "умолчанию); подписка со своим agent_batch_min главнее; сообщения владельца и срочные — всегда сразу",
    )
    project_set_p.add_argument(
        "--unsorted-accounts",
        default=None,
        metavar="N,M|off",
        help="проект слышит «Неразобранное» этих аккаунтов: подписки проекта получают сигнал о новых "
        "сообщениях в их беседах без проекта (номера через запятую; off — снять, по умолчанию)",
    )
    project_set_p.add_argument(
        "--workday",
        choices=("on", "off"),
        default=None,
        help="проект «по времени»: рабочий день кнопками «Начать день» / «Закончить день» (hub project day); "
        "off открывает день; по умолчанию off",
    )
    project_set_p.add_argument(
        "--card-route",
        default=None,
        metavar="ГРУППА=me|agent|auto,…",
        help="кто отвечает на карточки проекта (частично): me — владелец, agent — агент владельца, auto — «сразу да» "
        "(только permission, draft, press, join, requests, states, reminder)",
    )
    project_set_p.set_defaults(func=_cmd_project_set)
    project_list_p = project_sub.add_parser("list", help="проекты и политика внимания")
    project_list_p.set_defaults(func=_cmd_project_list)
    project_day_p = project_sub.add_parser(
        "day", help="рабочий день проекта «по времени»: open — начать, close — закончить"
    )
    project_day_p.add_argument("project_id", type=int)
    project_day_p.add_argument("action", choices=("open", "close"))
    project_day_p.set_defaults(func=_cmd_project_day)

    sub_p = sub.add_parser("sub", help="подписки сессий Claude Code на проекты и беседы (Ф13-1, Ф14-5)")
    sub_sub = sub_p.add_subparsers(dest="sub_command", required=True)
    sub_wait_p = sub_sub.add_parser(
        "wait",
        help="ждать сигнал подписки (для сессии, Bash в фоне)",
        description="Ждать сигнал подписки на проект (номер) или подписок сессии на беседы (--channels). "
        "Коды выхода: 0 — сигналы; 2 — отказ (нет профиля, 401/403/404, неверные аргументы); "
        "3 — подписка снята (410, смена разговора, реестр); 4 — вытеснено новым ожиданием; "
        "5 — сессии-предка нет.",
    )
    sub_wait_p.add_argument("sub_id", type=int, nargs="?", default=None, help="номер подписки на проект")
    sub_wait_p.add_argument(
        "--channels", action="store_true", help="ждать по подпискам ЭТОЙ сессии на беседы (одно ожидание)"
    )
    sub_wait_p.add_argument("--profile", default=None, help="профиль hub-mcp, которым подписалась сессия")
    sub_wait_p.add_argument("--url", default="http://127.0.0.1:8787/api/v1", help="адрес публичного API ядра")
    # Ф16-4: ожидание для сессии OpenCode — флаги подставляет плагин хаба (deploy/opencode/hub.ts).
    sub_wait_p.add_argument("--oc-session", default=None, help=argparse.SUPPRESS)
    sub_wait_p.add_argument("--oc-dir", default=None, help=argparse.SUPPRESS)
    # Ф74-2: то же для внешних Codex и Cursor — флаги подставляет хук хаба (`hub-ext-hook`); `--ext-pid` —
    # процесс движка (ожидатель хука Codex отвязан от дерева процессов).
    _ext_flags(sub_wait_p)
    sub_leave_p = sub_sub.add_parser(
        "leave",
        help="снять все подписки сессии OpenCode, Codex или Cursor (зовёт плагин хаба при конце сессии)",
    )
    sub_leave_p.add_argument("--oc-session", default=None, help="ses_… сессии OpenCode")
    sub_leave_p.add_argument("--oc-dir", default=None, help="каталог сессии OpenCode")
    _ext_flags(sub_leave_p)
    sub_leave_p.add_argument("--profile", default=None, help="профиль hub-mcp, которым подписалась сессия")
    sub_leave_p.add_argument("--url", default="http://127.0.0.1:8787/api/v1", help="адрес API ядра")
    sub_list_p = sub_sub.add_parser("list", help="живые подписки и завершённые за сутки (--days N — за N)")
    sub_list_p.add_argument(
        "--days", type=int, default=1, choices=range(1, 31), metavar="N", help="завершённые за N суток (1–30)"
    )
    sub_free_p = sub_sub.add_parser(
        "free",
        help="агенты вне проектов (сессии не в папке проекта, mode=direct): on — разрешить, off — снять "
        "живые подписки и отказывать новым",
    )
    sub_free_p.add_argument("state", choices=("on", "off"))
    sub_end_p = sub_sub.add_parser("end", help="снять подписку (как владелец): номер или c<номер> — на беседу")
    sub_end_p.add_argument("sub_id")
    sub_p.set_defaults(func=_cmd_sub)

    token_p = sub.add_parser("token", help="управление bearer-токенами")
    token_sub = token_p.add_subparsers(dest="token_command", required=True)
    token_create_p = token_sub.add_parser("create")
    token_create_p.add_argument("--name", required=True)
    token_create_p.add_argument("--kind", choices=("interactive", "background"), required=True)
    token_create_p.add_argument("--scopes", required=True, metavar="read,draft,send,organize")
    token_create_p.add_argument("--project-ids", default=None, metavar="1,2")
    token_create_p.add_argument("--ttl-days", type=int, default=None)
    token_create_p.set_defaults(func=_cmd_token_create)
    token_list_p = token_sub.add_parser("list", help="bearer-токены (без значений)")
    token_list_p.set_defaults(func=_cmd_token_list)
    token_revoke_p = token_sub.add_parser("revoke", help="отозвать bearer-токен")
    token_revoke_p.add_argument("token_id", type=int)
    token_revoke_p.set_defaults(func=_cmd_token_revoke)

    term_agent_p = sub.add_parser("term-agent", help="ключ агента владельца к терминалу хаба (скоуп term)")
    term_agent_sub = term_agent_p.add_subparsers(dest="term_agent_command", required=True)
    term_agent_token_p = term_agent_sub.add_parser(
        "token", help="выпустить ключ (прежний отзывается); файл <config_dir>/term-agent.key, 0600"
    )
    term_agent_token_p.add_argument("--ttl-days", type=int, default=None)
    term_agent_token_p.set_defaults(func=_cmd_term_agent)
    term_agent_key_p = term_agent_sub.add_parser("key", help="напечатать ключ (нет ключа — код 3)")
    term_agent_key_p.set_defaults(func=_cmd_term_agent)
    term_agent_sub.add_parser("revoke", help="отозвать ключ").set_defaults(func=_cmd_term_agent)
    digest_p = sub.add_parser("digest", help="сводка дня проекта: ключ клиента и показ для хука (Ф86-1)")
    digest_sub = digest_p.add_subparsers(dest="digest_command", required=True)
    digest_token_p = digest_sub.add_parser(
        "token", help="ключ клиента сводки (скоуп digest; прежний отзывается), <config_dir>/digest.key"
    )
    digest_token_p.add_argument("--ttl-days", type=int, default=None)
    digest_token_p.add_argument("--print", action="store_true", help="напечатать ключ")
    digest_token_p.set_defaults(func=_cmd_digest)
    digest_revoke_p = digest_sub.add_parser("revoke", help="отозвать ключ клиента сводки")
    digest_revoke_p.set_defaults(func=_cmd_digest)
    digest_show_p = digest_sub.add_parser(
        "show", help="блок <hub_digest> сводки проекта (нет — пустой вывод, код 0)"
    )
    digest_where = digest_show_p.add_mutually_exclusive_group(required=True)
    digest_where.add_argument("--project", type=int, default=None, help="номер проекта")
    digest_where.add_argument(
        "--cwd", default=None, help="папка: проект по самому длинному совпадению рабочей папки"
    )
    digest_show_p.add_argument("--day", default=None, help="ГГГГ-ММ-ДД (по умолчанию — сегодня по времени хаба)")
    digest_show_p.add_argument("--max", type=int, default=9500, help="предел блока, символов (200–100000)")
    digest_show_p.set_defaults(func=_cmd_digest)
    term_p = sub.add_parser("term", help="терминал хаба")
    term_sub = term_p.add_subparsers(dest="term_command", required=True)
    term_sub.add_parser("list", help="терминалы хаба").set_defaults(func=_cmd_term_list)

    human_agent_p = sub.add_parser(
        "human-agent", help="ответчик заявок «нужен человек»: отдельный токен со скоупом human"
    )
    human_agent_sub = human_agent_p.add_subparsers(dest="human_agent_command", required=True)
    human_agent_token_p = human_agent_sub.add_parser(
        "token", help="выпустить токен ответчика (значение печатается один раз)"
    )
    human_agent_token_p.add_argument(
        "--project",
        type=int,
        action="append",
        default=None,
        metavar="N",
        help="только этот проект (можно повторить)",
    )
    human_agent_token_p.add_argument("--name", default="human-agent")
    human_agent_token_p.add_argument("--ttl-days", type=int, default=None)
    human_agent_token_p.set_defaults(func=_cmd_human_agent_token)
    channel_p = sub.add_parser("channel", help="канал: показать, перенести в проект")
    channel_sub = channel_p.add_subparsers(dest="channel_command", required=True)
    channel_show_p = channel_sub.add_parser("show")
    channel_show_p.add_argument("channel_id", type=int)
    channel_show_p.set_defaults(func=_cmd_channel_show)
    channel_wait_p = channel_sub.add_parser(
        "wait", help="ждать новое сообщение беседы (живой разговор): JSON ответа; --follow — поток строк"
    )
    channel_wait_p.add_argument("channel_id", type=int)
    channel_wait_p.add_argument("--after", type=int, default=None, metavar="SEQ", help="курсор (seq)")
    channel_wait_p.add_argument("--timeout", type=int, default=60, metavar="S", help="секунды, 1..120")
    channel_wait_p.add_argument("--thread", default=None, metavar="EXT", help="только тред (ext родителя)")
    channel_wait_p.add_argument("--include-mine", action="store_true", help="будить и на свои сообщения")
    channel_wait_p.add_argument("--follow", action="store_true", help="ждать по кругу, строка на пробуждение")
    channel_wait_p.set_defaults(func=_cmd_channel_wait)
    channel_move_p = channel_sub.add_parser("move")
    channel_move_p.add_argument("channel_id", type=int)
    move_group = channel_move_p.add_mutually_exclusive_group(required=True)
    move_group.add_argument("--project", type=int, default=None, metavar="ID")
    move_group.add_argument("--unsorted", action="store_true", help="в «Неразобранное»")
    move_group.add_argument("--rules", action="store_true", help="вернуть под правила раскладки")
    channel_move_p.set_defaults(func=_cmd_channel_move)
    channel_archive_p = channel_sub.add_parser(
        "archive", help="беседы в архив (их открытые карточки закрываются); --undo — из архива"
    )
    channel_archive_p.add_argument("channel_ids", type=int, nargs="+", metavar="ID")
    channel_archive_p.add_argument("--undo", action="store_true", help="вернуть из архива")
    channel_archive_p.set_defaults(func=_cmd_channel_archive)
    channel_attention_p = channel_sub.add_parser(
        "attention", help="режим внимания беседы (auto — по политике проекта)"
    )
    channel_attention_p.add_argument("channel_id", type=int)
    channel_attention_p.add_argument("mode", choices=("react", "info", "auto"))
    channel_attention_p.set_defaults(func=_cmd_channel_attention)
    channel_autosend_p = channel_sub.add_parser(
        "autosend", help="беседы «без подтверждения» для роли сессии: ID --role РОЛЬ on|off; list — список"
    )
    channel_autosend_p.add_argument("target", metavar="ID|list")
    channel_autosend_p.add_argument("--role", default=None, metavar="РОЛЬ")
    channel_autosend_p.add_argument("state", nargs="?", choices=("on", "off"), default=None)
    channel_autosend_p.set_defaults(func=_cmd_channel_autosend)

    mcp_p = sub.add_parser("mcp", help="MCP-сервер хаба на stdio (Claude Code/OpenCode); `setup` — профиль")
    mcp_p.add_argument("--profile", default=None, help="имя профиля ([a-z0-9-]{1,32})")
    mcp_p.add_argument("--url", default="http://127.0.0.1:8787/api/v1", help="адрес публичного API ядра")
    mcp_sub = mcp_p.add_subparsers(dest="mcp_command")
    mcp_setup_p = mcp_sub.add_parser("setup", help="создать токен профиля и напечатать команды подключения")
    mcp_setup_p.add_argument("--profile", required=True)
    mcp_setup_p.add_argument("--projects", default="all", metavar="all|1,2,3")
    mcp_setup_p.add_argument("--organize", action="store_true", help="право переносить каналы (только all)")
    mcp_setup_p.add_argument("--attach", action="store_true", help="право класть файлы в черновики")
    mcp_attach_p = mcp_sub.add_parser(
        "attach", help="выдать/снять право класть файлы в черновики (токен не перевыпускается)"
    )
    mcp_attach_p.add_argument("--profile", required=True)
    mcp_attach_p.add_argument("state", choices=("on", "off"))
    mcp_p.set_defaults(func=_cmd_mcp)

    agent_p = sub.add_parser("agent", help="дежурный агент: разбор входящих (setup — токен, run — процесс)")
    agent_sub = agent_p.add_subparsers(dest="agent_command", required=True)
    agent_sub.add_parser("setup", help="токен агента, конфиг по умолчанию, каталог состояния")
    agent_run_p = agent_sub.add_parser("run", help="процесс разборщика (юнит hub-agent.service)")
    agent_run_p.add_argument(
        "--url", default="http://127.0.0.1:8787/api/v1", help="адрес публичного API ядра"
    )
    agent_sub.add_parser("status", help="курсор, отставание, счётчики, ошибки — без текстов")
    agent_p.set_defaults(func=_cmd_agent)

    transcribe_p = sub.add_parser(
        "transcribe", help="расшифровка голосовых локальной моделью (setup — токен и проверка, run — служба)"
    )
    transcribe_sub = transcribe_p.add_subparsers(dest="transcribe_command", required=True)
    transcribe_setup_p = transcribe_sub.add_parser(
        "setup", help="токен службы, настройки transcribe.json, проверка движка (без загрузки модели)"
    )
    transcribe_setup_p.add_argument(
        "--engine-python", help="интерпретатор движка (по умолчанию — venv диктовки)"
    )
    transcribe_setup_p.add_argument(
        "--model-dir",
        action="append",
        help="каталог модели faster-whisper (можно несколько; первый с model.bin)",
    )
    transcribe_setup_p.add_argument(
        "--device",
        choices=("cuda", "cpu"),
        help="устройство движка (Ф79-1): cuda — быстрый работник (голосовые первым и диктовка), "
        "cpu — медленный (голосовые, когда быстрого нет)",
    )
    transcribe_run_p = transcribe_sub.add_parser(
        "run", help="служба расшифровки (юнит hub-transcribe.service)"
    )
    transcribe_run_p.add_argument(
        "--url", default="http://127.0.0.1:8787/api/v1", help="адрес публичного API ядра"
    )
    transcribe_sub.add_parser("status", help="очередь, движок, последняя ошибка — без текстов")
    transcribe_p.set_defaults(func=_cmd_transcribe)

    listen_p = sub.add_parser(
        "listen", help="помощник на интервью: токен службы записи, «Подскажи» клавишей, состояние"
    )
    listen_sub = listen_p.add_subparsers(dest="listen_command", required=True)
    listen_sub.add_parser("setup", help="токен службы записи (скоуп listen) в <config>/listen/token, 0600")
    listen_hint_p = listen_sub.add_parser(
        "hint", help="«Подскажи» по идущему интервью (выход 2 — интервью не идёт, 78 — нет токена)"
    )
    listen_hint_p.add_argument(
        "--url", default="http://127.0.0.1:8787/api/v1", help="адрес публичного API ядра"
    )
    listen_sub.add_parser("status", help="служба жива ли, идущее интервью — без текста строк")
    listen_run_p = listen_sub.add_parser(
        "run", help="служба записи интервью: микрофон и звук звонка кусками в ядро (юнит hub-listen.service)"
    )
    listen_run_p.add_argument(
        "--url", default="http://127.0.0.1:8787/api/v1", help="адрес публичного API ядра"
    )
    listen_p.set_defaults(func=_cmd_listen)

    notify_p = sub.add_parser("notify", help="уведомления рабочего стола из ядра: токен, служба, состояние")
    notify_sub = notify_p.add_subparsers(dest="notify_command", required=True)
    notify_sub.add_parser("setup", help="токен спутника (скоуп notify) в <config>/notify.token, 0600")
    notify_run_p = notify_sub.add_parser(
        "run", help="показывать уведомления ядра через notify-send (юнит hub-notify.service)"
    )
    notify_status_p = notify_sub.add_parser("status", help="токен есть ли и принимает ли его ядро")
    for notify_cmd_p in (notify_run_p, notify_status_p):
        notify_cmd_p.add_argument(
            "--url", default="http://127.0.0.1:8787/api/v1", help="адрес публичного API ядра"
        )
    notify_p.set_defaults(func=_cmd_notify)

    # Ф9-1 (9a). Команд, которые решают запрос сессии, НЕТ (П3): только владелец в UI.
    work_p = sub.add_parser("work", help="хост сессий хаба (hub-work)")
    work_sub = work_p.add_subparsers(dest="work_command", required=True)
    work_setup_p = work_sub.add_parser(
        "setup", help="токен work:host, каталоги, файл окружения юнита, конфиг"
    )
    work_setup_p.add_argument(
        "--url",
        default="http://127.0.0.1:8787/api/v1",
        help="адрес публичного API ядра",
    )
    work_sub.add_parser("status", help="живые процессы сессий хаба, пауза, вход движков, правила")
    work_run_p = work_sub.add_parser("run", help="хост (юнит hub-work.service)")
    work_run_p.add_argument("--url", default="http://127.0.0.1:8787/api/v1", help="адрес публичного API ядра")
    work_guard_p = work_sub.add_parser("guard", help="гард-хук PreToolUse рабочей сессии (JSON на stdin)")
    work_guard_p.add_argument("--workdir", required=True)
    work_guard_p.add_argument("--log", default=None)
    work_p.set_defaults(func=_cmd_work)

    remote_p = sub.add_parser("remote", help="доступ с телефона: status — состояние, off — выключить и выйти")
    remote_sub = remote_p.add_subparsers(dest="remote_command", required=True)
    remote_sub.add_parser("status", help="включён ли, адреса, слушатель, неверные пароли за сутки")
    remote_sub.add_parser("off", help="выключить доступ с телефона и отозвать телефонные сессии")
    remote_p.set_defaults(func=_cmd_remote)

    engine_p = sub.add_parser(
        "engine", help="движок агента владельца и фактов (модель, усилие): показать, set — переключить"
    )
    engine_sub = engine_p.add_subparsers(dest="engine_command")
    engine_set_p = engine_sub.add_parser("set", help="переключить движок (то же, что переключатель в UI)")
    engine_set_p.add_argument("engine", choices=["claude", "opencode", "codex", "cursor"])
    engine_set_p.add_argument("--model", default=None, help="модель движка (\"\" — умолчание движка)")
    engine_set_p.add_argument("--effort", default=None, help="усилие (\"\" — умолчание движка)")
    facts_p = engine_sub.add_parser(
        "facts", help="свой движок службы фактов о людях: показать; set — задать; off — как у агента"
    )
    facts_sub = facts_p.add_subparsers(dest="facts_command")
    facts_set_p = facts_sub.add_parser("set", help="задать движок, модель и усилие фактов")
    facts_set_p.add_argument("engine", choices=["claude", "opencode", "codex", "cursor"])
    facts_set_p.add_argument("--model", default=None, help='модель движка ("" — умолчание движка)')
    facts_set_p.add_argument("--effort", default=None, help='усилие ("" — умолчание движка)')
    facts_sub.add_parser("off", help="снять свой выбор: факты снова на движке агента")
    fallback_p = engine_sub.add_parser(
        "fallback", help="запасной движок агента владельца или фактов: показать; set; off; return — на основной"
    )
    fallback_p.add_argument("who", choices=["agent", "facts"])
    fallback_sub = fallback_p.add_subparsers(dest="fallback_command")
    fallback_set_p = fallback_sub.add_parser("set", help="задать запасной движок, модель и усилие")
    fallback_set_p.add_argument("engine", choices=["claude", "opencode", "codex", "cursor"])
    fallback_set_p.add_argument("--model", default=None, help='модель движка ("" — умолчание движка)')
    fallback_set_p.add_argument("--effort", default=None, help='усилие ("" — умолчание движка)')
    fallback_sub.add_parser("off", help="без запасного (снимает и «на запасном»)")
    fallback_sub.add_parser("return", help="вернуть на основной движок")
    engine_sub.add_parser(
        "setup",
        help="записать engine.json: путь к opencode, версия, каталоги node, имена MCP (из своего терминала)",
    )
    mcp_export_p = engine_sub.add_parser(
        "mcp-export",
        help="набор MCP Claude Code в формате Cursor, OpenCode или Codex (секреты — только в файл --out)",
    )
    mcp_export_p.add_argument("--format", required=True, choices=["cursor", "opencode", "codex"])
    mcp_export_p.add_argument("--dir", default=None, help="папка проекта (её выключенные серверы)")
    mcp_export_p.add_argument("--out", default=None, help="файл с настоящими секретами (0600)")
    engine_p.set_defaults(func=_cmd_engine)

    autopilot_p = sub.add_parser(
        "autopilot", help="выключатели автопилота сессий: показать; hold-all — поставить все (авария)"
    )
    autopilot_sub = autopilot_p.add_subparsers(dest="autopilot_command")
    autopilot_sub.add_parser("hold-all", help="поставить все выключатели: общие и по проектам")
    autopilot_p.set_defaults(func=_cmd_autopilot)

    load_p = sub.add_parser(
        "load",
        help="нагрузка ноутбука: показать; run … -- команда — записать на время команды (снимется сама)",
    )
    load_p.add_argument("--json", action="store_true", help="сырой ответ ядра")
    load_sub = load_p.add_subparsers(dest="load_command")

    def _load_claim_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--what", required=True, help="что идёт: «сборка образов», «полный прогон тестов»")
        p.add_argument("--cores", type=int, required=True, help="сколько ядер займёт")
        p.add_argument("--mem-gb", type=float, required=True, help="сколько памяти займёт, ГБ")
        p.add_argument("--minutes", type=int, required=True, help="сколько минут продлится (1…240)")
        p.add_argument("--who", default=None, help="кто запускает (по умолчанию — имя текущей папки)")

    load_run_p = load_sub.add_parser(
        "run", help="записать, запустить команду, снять запись при любом исходе; код выхода — код команды"
    )
    _load_claim_args(load_run_p)
    load_run_p.add_argument("command", nargs=argparse.REMAINDER, metavar="-- команда…")
    load_add_p = load_sub.add_parser(
        "add", help="записать уже запущенный долгий процесс (стенд в фоне): запись живёт, пока жив его pid"
    )
    load_add_p.add_argument("--pid", type=int, required=True, help="pid процесса")
    _load_claim_args(load_add_p)
    load_done_p = load_sub.add_parser("done", help="снять запись раньше срока")
    load_done_p.add_argument("claim_id", type=int, help="номер записи (печатает `hub load add`)")
    load_p.set_defaults(func=_cmd_load)

    trace_p = sub.add_parser(
        "trace", help="полная переписка по карточке или по каналу владельца за окно (только чтение)"
    )
    trace_p.add_argument(
        "ref", nargs="?", default=None, help="ref карточки или поручения: human:19, errand:3871…"
    )
    trace_p.add_argument("--around", default=None, help="окно вокруг времени «ГГГГ-ММ-ДД ЧЧ:ММ» (местное)")
    trace_p.add_argument("--min", type=int, default=15, help="полуширина окна, минут (по умолчанию 15)")
    trace_p.add_argument("--project", type=int, default=None, help="только этот проект (для --around)")
    trace_p.add_argument("--json", action="store_true", help="сырой ответ ядра")
    trace_p.set_defaults(func=_cmd_trace)

    machine_p = sub.add_parser(
        "machine", help="машины, с которых сессии ходят к ядру: list; name <host|hostname> <имя> — назвать"
    )
    machine_p.add_argument("--json", action="store_true", help="сырой ответ ядра")
    machine_sub = machine_p.add_subparsers(dest="machine_command")
    machine_sub.add_parser("list", help="список машин (машина ядра первой)")
    machine_name_p = machine_sub.add_parser("name", help="задать имя машины (пустая строка — снять)")
    machine_name_p.add_argument("machine", help="хеш машины (16 hex), её hostname или нынешнее имя")
    machine_name_p.add_argument("name", help="имя, до 32 символов; \"\" — снять")
    machine_p.set_defaults(func=_cmd_machine)

    status_p = sub.add_parser("status", help="статус аккаунтов и очередей")
    status_p.set_defaults(func=_cmd_status)

    account_resume_p = account_sub.add_parser("resume")
    account_resume_p.add_argument("account_id", type=int)
    account_resume_p.set_defaults(func=_cmd_account_resume)
    account_set_p = account_sub.add_parser(
        "set", help="правка конфига аккаунта: ключ=значение (JSON; null — удалить ключ)"
    )
    account_set_p.add_argument("account_id", type=int)
    account_set_p.add_argument("pairs", nargs="+", metavar="k=v")
    account_set_p.set_defaults(func=_cmd_account_set)
    account_rm_p = account_sub.add_parser(
        "rm", help="убрать пустой аккаунт (заведён, но ни разу не подключался и без данных)"
    )
    account_rm_p.add_argument("account_id", type=int)
    account_rm_p.set_defaults(func=_cmd_account_rm)

    # Ф63-1: правила проекта для входящих.
    rules_p = sub.add_parser("rules", help="правила проекта для входящих")
    rules_sub = rules_p.add_subparsers(dest="rules_command", required=True)
    rules_list_p = rules_sub.add_parser("list", help="правила проекта")
    rules_list_p.add_argument("--project", type=int, required=True)
    rules_list_p.set_defaults(func=_cmd_rules_list)
    rules_add_p = rules_sub.add_parser(
        "add", help='правило от владельца: --json \'{"title":…,"from":[…],"subject":[…],"archive":true}\''
    )
    rules_add_p.add_argument("--project", type=int, required=True)
    rules_add_p.add_argument("--json", required=True, help="поля правила (как в «⋯ → Раскладка»)")
    rules_add_p.set_defaults(func=_cmd_rules_add)
    rules_rm_p = rules_sub.add_parser("rm", help="снять правило")
    rules_rm_p.add_argument("--project", type=int, required=True)
    rules_rm_p.add_argument("rule_id", type=int)
    rules_rm_p.set_defaults(func=_cmd_rules_rm)

    map_p = sub.add_parser("map", help="раскладка каналов по проектам (правила)")
    map_sub = map_p.add_subparsers(dest="map_command", required=True)

    map_list_p = map_sub.add_parser("list")
    map_list_p.add_argument("--account", type=int, default=None)
    map_list_p.set_defaults(func=_cmd_map_list)

    map_add_p = map_sub.add_parser("add")
    map_add_p.add_argument("--account", type=int, required=True)
    map_add_p.add_argument("--project", type=int, required=True)
    map_group = map_add_p.add_mutually_exclusive_group(required=True)
    map_group.add_argument("--folder", type=int, default=None, metavar="ID")
    map_group.add_argument("--channel", default=None, metavar="EXT_ID")
    # Раздел G спеки 2b: раскладка «весь аккаунт» — взаимоисключающая с --folder/--channel (та же группа).
    map_group.add_argument("--all", action="store_true")
    # Спека 5a, F: метка Gmail (`attrs.labels` треда) — взаимоисключающая с --folder/--channel/--all.
    map_group.add_argument("--label", default=None, metavar="NAME")
    # Почта 24.09: домен отправителя (`attrs.from_domains` треда — домены From всех писем треда).
    map_group.add_argument("--from-domain", default=None, metavar="DOMAIN")
    # None -> ядро решает умолчание (mapping.default_priority: -100 для --all, иначе 0), не 0 всегда.
    map_add_p.add_argument("--priority", type=int, default=None)
    map_add_p.set_defaults(func=_cmd_map_add)

    map_rm_p = map_sub.add_parser("rm")
    map_rm_p.add_argument("rule_id", type=int)
    map_rm_p.set_defaults(func=_cmd_map_rm)

    folders_p = sub.add_parser("folders", help="список папок Telegram аккаунта (account_meta.tg_folders)")
    folders_p.add_argument("--account", type=int, required=True)
    folders_p.set_defaults(func=_cmd_folders)

    connector_p = sub.add_parser("connector", help="запустить внешний цикл коннектора аккаунта")
    connector_p.add_argument("account_id", type=int)
    connector_p.set_defaults(func=_cmd_connector)

    feed_p = sub.add_parser("feed", help="лента от внешней программы (Ф53-1)")
    feed_sub = feed_p.add_subparsers(dest="feed_command", required=True)
    feed_push_p = feed_sub.add_parser("push", help="положить запись в беседу ленты (аккаунт source=feed)")
    feed_push_p.add_argument("--account", type=int, required=True, help="id аккаунта-ленты")
    feed_push_p.add_argument("--ext", required=True, help="номер записи у источника (против дублей)")
    feed_push_p.add_argument("--feed", default="main", help="ключ ленты внутри аккаунта [a-z0-9_-]{1,40}")
    feed_push_p.add_argument("--feed-title", default=None, help="название беседы (по умолчанию — аккаунта)")
    feed_push_p.add_argument("--sender", default=None, help="имя отправителя (по умолчанию — название ленты)")
    feed_push_p.add_argument("--ts", type=int, default=None, help="время записи, мс (по умолчанию — сейчас)")
    feed_push_p.add_argument("--note", action="store_true", help="служебная строка: без сигнала")
    feed_push_p.add_argument(
        "--alert", action="store_true", help="запись лично владельцу: «Ждёт тебя» и уведомление (СТОП сторожа)"
    )
    feed_push_p.add_argument("--text", default=None, help="текст (иначе со stdin)")
    feed_push_p.set_defaults(func=_cmd_feed_push)

    backup_p = sub.add_parser("backup", help="сделать шифрованный бэкап (или задать recipient)")
    backup_mode = backup_p.add_mutually_exclusive_group()
    backup_mode.add_argument(
        "--set-recipient", metavar="age1...", default=None, help="записать публичный ключ age получателя"
    )
    backup_mode.add_argument(
        "--snapshot-dir",
        metavar="DIR",
        default=None,
        help="открытый снимок БД и сессий для restic в пустой каталог /dev/shm/hub-restic-* (0700)",
    )
    backup_mode.add_argument(
        "--hold-outbox",
        metavar="DATA_DIR",
        default=None,
        help="после восстановления: строки очереди в пути -> «могло уйти» (ядро в каталоге не запущено)",
    )
    backup_p.set_defaults(func=_cmd_backup)

    watch_p = sub.add_parser("watch", help="разовая проверка состояния (вызывается таймером systemd)")
    watch_p.add_argument("--port", type=int, default=_PUBLIC_PORT, help="порт публичного приложения")
    watch_p.set_defaults(func=_cmd_watch)

    return parser


def load_main() -> None:
    """Ф52-1: вход `hub-load` — то же, что `hub load …` (`hub-load`, `hub-load run … -- <команда>`,
    `hub-load add …`, `hub-load done <id>`, `--json`). Отдельное имя — чтобы учёт нагрузки был доступен
    сессии хаба при любом положении выключателя `hub_cli` (его шаблоны закрывают `hub …`)."""
    main(["load", *sys.argv[1:]])


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return
    args.func(args)


# Ф33-4, К8: спутники ноутбука пишут свой токен локально — это верно, их не ограничиваем. `mcp` — тоже:
# MCP-процессы внешних сессий живут на ноутбуке и читают токен профиля из его конфига. `transcribe` (Ф79-1) —
# служба расшифровки стоит на обеих машинах (ноутбук — быстрый работник на cuda) со своим токеном у каждой.
_SATELLITE_SETUP = frozenset({"listen", "notify", "mcp", "transcribe"})


def _writes_machine_files(args: argparse.Namespace) -> bool:
    """К8: `hub auth …`, `hub backup --set-recipient`, `hub <служба> setup` пишут файлы на машину, где идёт
    ядро; на спутнике (есть `site.json`, нет флага `primary`) они уехали бы не туда."""
    command = getattr(args, "command", None)
    if command == "auth":
        return True
    if command == "backup":
        return getattr(args, "set_recipient", None) is not None
    # `hub load run -- <команда>` кладёт свою команду списком в тот же `command` — это не подкоманда хаба.
    if not isinstance(command, str) or command in _SATELLITE_SETUP:
        return False
    return getattr(args, f"{command.replace('-', '_')}_command", None) == "setup"
