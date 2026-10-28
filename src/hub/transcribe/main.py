"""Команды службы расшифровки: `hub transcribe setup|run|status` (Ф14-6, docs/architecture.md). `setup` запускает
владелец (боевые каталоги агенту-разработчику закрыты); токен ядра не печатается и
не попадает в аргументы — только в файл 0600."""

import asyncio
import json
import logging
import signal
import socket
import subprocess
from datetime import datetime
from pathlib import Path

import httpx

from . import files
from .core_api import TranscribeCore
from .service import ENGINE_PATH, Worker, run_service

_logger = logging.getLogger("hub.transcribe")

EXIT_NO_TOKEN = 78
CHECK_TIMEOUT_S = 60


class SetupError(Exception):
    pass


def _host() -> str:
    return socket.gethostname()


def _foreign(meta: dict | None, config_dir: Path) -> bool:
    """`token.json` — не этой машины: другой `host`, либо `host` нет (до Ф79-1) и эта машина — спутник."""
    if not meta:
        return False
    host = meta.get("host")
    if host is None:
        satellite = False
        return satellite
    return host != _host()


def _old_token_to_revoke(admin: httpx.Client, config_dir: Path) -> int | None:
    """Как `hub agent setup`: отзывать только токен из `token.json`, и только если его имя ровно
    `transcribe:worker` и он ещё не отозван.

    Ф79-1: служба стоит на обеих машинах, у каждой свой токен. Отзывается только токен, выпущенный на этой
    машине (`host` в `token.json`). `token.json` без `host` (до Ф79-1; мог приехать копией конфига с другой
    машины) — отзывается, только если эта машина не спутник: на спутнике этим токеном, скорее всего, работает
    служба главной."""
    meta = files.read_json(files.token_meta_path(config_dir))
    old_id = meta.get("token_id") if meta else None
    if not isinstance(old_id, int) or isinstance(old_id, bool):
        return None
    if _foreign(meta, config_dir):
        return None
    r = admin.get("/admin/tokens")
    r.raise_for_status()
    for token in r.json():
        if token["id"] == old_id and token["name"] == files.TOKEN_NAME and not token["revoked"]:
            return old_id
    return None


def check_engine(config: dict, *, runner=subprocess.run) -> tuple[bool, str]:
    """`engine.py --check` интерпретатором движка: faster_whisper импортируется, модель на месте (без
    загрузки на видеокарту)."""
    argv = [files.expand(config["engine_python"]), str(ENGINE_PATH), "--check"]
    for path in config.get("model_dirs") or []:
        argv += ["--model-dir", files.expand(path)]
    try:
        proc = runner(argv, capture_output=True, text=True, timeout=CHECK_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"движок не запускается: {type(exc).__name__} ({argv[0]})"
    try:
        info = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False, f"движок не ответил на --check (код {proc.returncode})"
    if not info.get("ok"):
        return False, f"движок не готов: {info.get('error') or 'неизвестно'}"
    return True, (
        f"движок готов: python {info.get('python')}, faster-whisper {info.get('faster_whisper')}, "
        f"модель {info.get('model')}, cuBLAS {'есть' if info.get('cublas') else 'нет (счёт на cpu)'}"
    )


def run_setup(
    admin: httpx.Client,
    *,
    config_dir: Path,
    data_dir: Path,
    engine_python: str | None = None,
    model_dirs: list[str] | None = None,
    device: str | None = None,
    runner=subprocess.run,
) -> tuple[bool, str]:
    """Токен `background` `transcribe:worker` (`read transcribe`, все проекты) через админ-сокет: создать ->
    записать -> отозвать прежний. Настройки `<config>/transcribe.json` (флаги — поверх). Каталог данных
    (0700) — сразу (иначе `ReadWritePaths` юнита роняет его с 226/NAMESPACE). Затем `engine.py --check`."""
    files.private_dir(files.config_service_dir(config_dir))
    files.private_dir(files.data_service_dir(data_dir))
    files.private_dir(files.tmp_dir(data_dir))
    old_id = _old_token_to_revoke(admin, config_dir)
    old_meta = files.read_json(files.token_meta_path(config_dir))
    kept_id = old_meta.get("token_id") if old_meta and _foreign(old_meta, config_dir) else None
    r = admin.post(
        "/admin/tokens",
        json={
            "name": files.TOKEN_NAME,
            "kind": "background",
            "scopes": list(files.TOKEN_SCOPES),
            "project_ids": None,
            "ttl_days": None,
        },
    )
    if r.status_code != 200:
        raise SetupError(f"ядро не создало токен: HTTP {r.status_code}")
    body = r.json()
    files.write_secret(files.token_path(config_dir), body["token"])
    files.atomic_write(
        files.token_meta_path(config_dir),
        json.dumps({"token_id": body["token_id"], "host": _host()}, ensure_ascii=False) + "\n",
    )
    lines = [
        f"Токен службы расшифровки: id={body['token_id']}, скоупы {' '.join(files.TOKEN_SCOPES)}, "
        "все проекты (background).",
        f"Токен записан в {files.token_path(config_dir)} (0600).",
    ]
    if isinstance(kept_id, int) and not isinstance(kept_id, bool):
        lines.append(
            f"Прежний токен (id={kept_id}) не отзывался: он выпущен не на этой машине — им может работать "
            "служба другой машины."
        )
    if old_id is not None and old_id != body["token_id"]:
        rr = admin.post(f"/admin/tokens/{old_id}/revoke")
        lines.append(
            f"Прежний токен (id={old_id}) отозван."
            if rr.status_code == 200
            else f"Прежний токен id={old_id} отозвать не удалось (HTTP {rr.status_code})."
        )
    config = files.load_config(config_dir)
    if engine_python:
        config["engine_python"] = engine_python
    if model_dirs:
        config["model_dirs"] = list(model_dirs)
    if device:
        config["device"] = device
    files.write_config(config_dir, config)
    lines.append(
        f"Настройки: {files.config_path(config_dir)} (устройство {config.get('device')}: "
        + (
            "быстрый работник — голосовые первым и диктовка)."
            if files.is_fast(config)
            else "медленный работник — голосовые, когда быстрого нет; диктовку не берёт)."
        )
    )
    ok, text = check_engine(config, runner=runner)
    lines.append(text if ok else f"ВНИМАНИЕ: {text}")
    lines += [
        f"Каталог данных: {files.data_service_dir(data_dir)} (0700).",
        "",
        "Дальше: systemctl --user enable --now hub-transcribe.service",
    ]
    return ok, "\n".join(lines)


def _fmt_ms(ts) -> str:
    if not isinstance(ts, int):
        return "—"
    return datetime.fromtimestamp(ts / 1000).strftime("%d.%m %H:%M:%S")


def status_text(data_dir: Path, queue: dict | None) -> str:
    """Очередь (из `/admin/watch` ядра), движок жив/нет и последние ошибки (из `state.json` службы) — без
    текстов расшифровок."""
    lines = []
    if queue is None:
        lines.append("очередь: ядро не ответило")
    else:
        lines.append(
            f"очередь: ждут {queue.get('queued', 0)}, в работе {queue.get('working', 0)}, "
            f"не удалось за сутки {queue.get('failed_24h', 0)}; старейшая ждёт с "
            f"{_fmt_ms(queue.get('oldest_queued_ts'))}"
        )
        if not queue.get("configured"):
            lines.append("служба не заведена: hub transcribe setup")
    state = files.load_state(data_dir)
    if state is None:
        lines.append("служба: состояния нет (ещё не запускалась)")
        return "\n".join(lines)
    alive = "жив (pid {})".format(state.get("engine_pid")) if state.get("engine_alive") else "выгружен"
    lines.append(f"движок: {alive}; обновлено {_fmt_ms(state.get('updated_ts'))}")
    lines.append(f"последняя расшифровка: {_fmt_ms(state.get('last_ok_ts'))}")
    dictate = state.get("dictate")
    if isinstance(dictate, dict):
        lines.append(
            "диктовка (Ф79-1): движок {}, кусков {} (не вышло {}, поздно {}), последний {}".format(
                "жив" if dictate.get("engine_alive") else "выгружен",
                int(dictate.get("ok") or 0),
                int(dictate.get("failed") or 0),
                int(dictate.get("late") or 0),
                _fmt_ms(dictate.get("last_ok_ts")),
            )
        )
    lines.append("счётчики: " + json.dumps(state.get("counters") or {}, ensure_ascii=False, sort_keys=True))
    errors = state.get("errors") or []
    if errors:
        last = errors[-1]
        lines.append(f"последняя ошибка: {_fmt_ms(last.get('ts'))} {last.get('where')}: {last.get('error')}")
    return "\n".join(lines)


def build_dictate(*, core, config: dict, data_dir: Path, worker: Worker):
    """Ф79-1, раздел D: цикл диктовки — только у быстрой службы (`device=cuda`) и при `dictate: true`."""
    if not (files.is_fast(config) and config.get("dictate", True) is not False):
        return None
    from .dictate import DictateWorker

    return DictateWorker(core=core, config=config, data_dir=data_dir, state=worker.state, save=worker.save)


def run(*, config_dir: Path, data_dir: Path, base_url: str) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if files.read_secret(files.token_path(config_dir)) is None:
        _logger.error("transcribe: нет токена ядра — hub transcribe setup")
        return EXIT_NO_TOKEN
    config = files.load_config(config_dir)
    core = TranscribeCore(token_file=files.token_path(config_dir), base_url=base_url)
    worker = Worker(core=core, config=config, data_dir=data_dir)
    dictate = build_dictate(core=core, config=config, data_dir=data_dir, worker=worker)
    _logger.info(
        "transcribe: старт (%s), выгрузка движка через %s с простоя%s",
        "быстрый: cuda" if worker.fast else "медленный: " + str(config.get("device") or "cpu"),
        worker.idle_unload_s,
        f"; диктовка — свой движок, выгрузка через {dictate.idle_unload_s} с" if dictate else "",
    )

    async def _main() -> int:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        try:
            return await run_service(stop, worker, dictate)
        finally:
            await core.aclose()

    return asyncio.run(_main())
