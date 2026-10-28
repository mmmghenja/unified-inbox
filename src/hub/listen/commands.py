"""`hub listen setup|hint|status` (Ф60-1, docs/architecture.md, 5 и П7). `setup`
запускает владелец (боевые каталоги агенту-разработчику закрыты): токен ядра не печатается и не попадает в
аргументы — только в файл 0600. `hint` зовёт глобальная клавиша GNOME (`scripts/hub-interview-hotkey.sh`)."""

import json
from datetime import datetime
from pathlib import Path

import httpx

from . import files

EXIT_NO_INTERVIEW = 2  # `hint`: интервью не идёт
EXIT_NO_TOKEN = 78  # нет токена — как у служб хаба (юнит без перезапуска)
HINT_TIMEOUT_S = 10.0

TEXT_HINT_OK = "подсказка запрошена"
TEXT_HINT_QUEUED = "подсказка уже запрошена — ждёт сессию"
TEXT_NO_INTERVIEW = "интервью не идёт"
TEXT_NO_TOKEN = "токена службы записи нет: hub listen setup"  # noqa: S105 — текст, не секрет


class SetupError(Exception):
    pass


def _old_token_to_revoke(admin: httpx.Client, config_dir: Path) -> int | None:
    """Отзывать только токен из `token.json`, и только если его имя ровно `listen:recorder` и он не
    отозван."""
    meta = files.read_json(files.token_meta_path(config_dir))
    old_id = meta.get("token_id") if meta else None
    if not isinstance(old_id, int) or isinstance(old_id, bool):
        return None
    r = admin.get("/admin/tokens")
    r.raise_for_status()
    for token in r.json():
        if token["id"] == old_id and token["name"] == files.TOKEN_NAME and not token["revoked"]:
            return old_id
    return None


def run_setup(admin: httpx.Client, *, config_dir: Path, data_dir: Path) -> str:
    """Токен `background` `listen:recorder` (ровно `listen`, все проекты) через админ-сокет: создать ->
    записать в `<config_dir>/listen/token` (0600) -> отозвать прежний. Каталог данных (0700) — сразу (иначе
    `ReadWritePaths` юнита роняет его с 226/NAMESPACE)."""
    files.private_dir(files.config_service_dir(config_dir))
    files.private_dir(files.data_service_dir(data_dir))
    old_id = _old_token_to_revoke(admin, config_dir)
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
    files.atomic_write(files.token_meta_path(config_dir), json.dumps({"token_id": body["token_id"]}) + "\n")
    lines = [
        f"Токен службы записи интервью: id={body['token_id']}, скоуп listen, все проекты (background).",
        f"Токен записан в {files.token_path(config_dir)} (0600).",
    ]
    if old_id is not None and old_id != body["token_id"]:
        rr = admin.post(f"/admin/tokens/{old_id}/revoke")
        lines.append(
            f"Прежний токен (id={old_id}) отозван."
            if rr.status_code == 200
            else f"Прежний токен id={old_id} отозвать не удалось (HTTP {rr.status_code})."
        )
    lines += [
        f"Каталог данных: {files.data_service_dir(data_dir)} (0700).",
        "",
        "Дальше: systemctl --user enable --now hub-listen.service",
    ]
    return "\n".join(lines)


def hint(*, config_dir: Path, base_url: str = files.DEFAULT_URL, transport=None) -> tuple[int, str]:
    """«Подскажи» глобальной клавишей: `POST /listen/hint` токеном службы. Возвращает (код выхода, строка):
    0 — запрошена (или уже ждёт сессию), 2 — интервью не идёт, 78 — нет токена, 1 — ядро недоступно или
    отказало."""
    token = files.read_token(config_dir)
    if token is None:
        return EXIT_NO_TOKEN, TEXT_NO_TOKEN
    kwargs = {"transport": transport} if transport is not None else {}
    try:
        with httpx.Client(base_url=base_url, timeout=HINT_TIMEOUT_S, trust_env=False, **kwargs) as client:
            r = client.post("/listen/hint", headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        return 1, f"ядро недоступно ({type(exc).__name__})"
    if r.status_code == 404:
        return EXIT_NO_INTERVIEW, TEXT_NO_INTERVIEW
    if r.status_code != 200:
        return 1, f"ядро отказало: HTTP {r.status_code}"
    return 0, TEXT_HINT_QUEUED if r.json().get("queued") else TEXT_HINT_OK


def _ago(seen_ms, now_ms) -> str:
    if not isinstance(seen_ms, int) or not isinstance(now_ms, int):
        return "не опрашивала с запуска ядра"
    return f"опрашивала {max(0, now_ms - seen_ms) // 1000} с назад"


def status_text(config_dir: Path, data: dict | None) -> str:
    """Токен есть/нет, служба жива/нет (по опросам ядра), открытое интервью — без текста строк."""
    has_token = files.read_token(config_dir) is not None
    lines = ["Токен службы: есть" if has_token else f"Токен службы: нет ({TEXT_NO_TOKEN})"]
    if data is None:
        lines.append("Ядро не отвечает — состояние неизвестно.")
        return "\n".join(lines)
    alive = "жива" if data.get("listener_alive") else "не видна"
    lines.append(f"Служба записи: {alive}, {_ago(data.get('listener_seen_ms'), data.get('now_ms'))}.")
    item = data.get("interview")
    if not item:
        lines.append("Интервью не идёт.")
        return "\n".join(lines)
    started = datetime.fromtimestamp(item["started_ts"] / 1000).strftime("%d.%m %H:%M")
    mode = "по ходу" if item["mode"] == "live" else "по кнопке"
    status = {"active": "идёт", "paused": "на паузе"}.get(item["status"], item["status"])
    lines.append(
        f"Интервью #{item['id']} в сессии #{item['session_id']}: {status}, {mode}, собеседник "
        f"{item['them_lang']}, с {started}, строк {item['lines']}, неподанных реплик {item['queued_says']}."
    )
    if item.get("reason"):
        lines.append(f"Причина: {item['reason']}.")
    if item.get("warn"):
        lines.append(f"Предупреждение службы: {item['warn']}")
    if item.get("wait_reason"):
        lines.append(f"Подача ждёт: {item['wait_reason']}.")
    return "\n".join(lines)
