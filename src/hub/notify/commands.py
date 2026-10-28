"""`hub notify setup|status` (Ф33-2). `setup` запускает владелец: токен не печатается и не попадает в аргументы —
только в файл 0600 (образец — `hub listen setup`)."""

import json
from pathlib import Path

import httpx

from . import files


class SetupError(Exception):
    pass


def _old_token_to_revoke(admin: httpx.Client, config_dir: Path) -> int | None:
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


def run_setup(admin: httpx.Client, *, config_dir: Path) -> str:
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
        f"Токен спутника уведомлений: id={body['token_id']}, скоуп notify, все проекты (background).",
        f"Токен записан в {files.token_path(config_dir)} (0600).",
    ]
    if old_id is not None and old_id != body["token_id"]:
        rr = admin.post(f"/admin/tokens/{old_id}/revoke")
        lines.append(
            f"Прежний токен (id={old_id}) отозван."
            if rr.status_code == 200
            else f"Прежний токен id={old_id} отозвать не удалось (HTTP {rr.status_code})."
        )
    lines += ["", "Дальше: systemctl --user enable --now hub-notify.service"]
    return "\n".join(lines)


def status(config_dir: Path, *, base_url: str = files.DEFAULT_URL, transport=None) -> str:
    """Токен есть/нет, принимает ли его ядро (быстрый запрос без `after`, без ожидания)."""
    token = files.read_token(config_dir)
    if token is None:
        return "Токен спутника: нет (hub notify setup)"
    lines = ["Токен спутника: есть"]
    kwargs = {"transport": transport} if transport is not None else {}
    try:
        with httpx.Client(base_url=base_url, timeout=5.0, trust_env=False, **kwargs) as client:
            r = client.get("/notify/wait", headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        lines.append(f"Ядро не отвечает ({exc.__class__.__name__}).")
        return "\n".join(lines)
    if r.status_code in (401, 403):
        lines.append("Ядро токен не принимает (отозван?): hub notify setup")
    elif r.status_code != 200:
        lines.append(f"Ядро ответило HTTP {r.status_code}.")
    else:
        lines.append(f"Ядро отвечает, последний номер уведомления: {r.json().get('last')}.")
    return "\n".join(lines)
