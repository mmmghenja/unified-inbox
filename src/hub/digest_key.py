"""Ключ клиента сводки дня (Ф86-1, C1.3): bearer ровно `{digest}` (`background`, все проекты, без `attach`).
Хранится в `<config_dir>/digest.key` (0600) с номером токена: `{"token_id": N, "key": "hub_…"}`.

`hub digest token [--ttl-days N] [--print]` — выпуск (прежний ключ отзывается), `hub digest revoke` — отзыв.
Клиент (`day-digest.py` сисадмина) читает ключ из файла или получает его `--print` один раз."""

import json
from pathlib import Path

import httpx

from .agent.files import atomic_write, read_json

KEY_FILE = "digest.key"
TOKEN_NAME = "digest"  # noqa: S105 — имя токена в ядре, не секрет


class DigestKeyError(Exception):
    pass


def key_path(config_dir: Path) -> Path:
    return config_dir / KEY_FILE


def read_key(config_dir: Path) -> tuple[int, str] | None:
    data = read_json(key_path(config_dir))
    if not data:
        return None
    token_id, key = data.get("token_id"), data.get("key")
    if not isinstance(token_id, int) or isinstance(token_id, bool) or not isinstance(key, str) or not key:
        return None
    return token_id, key


def issue(admin: httpx.Client, config_dir: Path, *, ttl_days: int | None) -> tuple[str, str]:
    """-> (строка для человека, ключ). Прежний ключ отзывается."""
    previous = read_key(config_dir)
    r = admin.post(
        "/admin/tokens",
        json={
            "name": TOKEN_NAME,
            "kind": "background",
            "scopes": ["digest"],
            "project_ids": None,
            "ttl_days": ttl_days,
        },
    )
    if r.status_code == 422:
        raise DigestKeyError(str(r.json().get("detail")))
    if r.status_code != 200:
        raise DigestKeyError(f"ядро не выпустило ключ: HTTP {r.status_code}")
    body = r.json()
    atomic_write(
        key_path(config_dir), json.dumps({"token_id": body["token_id"], "key": body["token"]}) + "\n"
    )
    where = key_path(config_dir)
    lines = [f"ключ сводки дня выпущен #{body['token_id']} ({where}), отзыв: hub digest revoke"]
    if previous is not None and previous[0] != body["token_id"]:
        rr = admin.post(f"/admin/tokens/{previous[0]}/revoke")
        lines.append(
            f"прежний ключ #{previous[0]} отозван"
            if rr.status_code in (200, 404)
            else f"прежний ключ #{previous[0]} отозвать не удалось (HTTP {rr.status_code})"
        )
    return "\n".join(lines), body["token"]


def revoke(admin: httpx.Client, config_dir: Path) -> str:
    current = read_key(config_dir)
    if current is None:
        return "ключа нет"
    r = admin.post(f"/admin/tokens/{current[0]}/revoke")
    if r.status_code not in (200, 404):
        raise DigestKeyError(f"ключ #{current[0]} отозвать не удалось (HTTP {r.status_code})")
    key_path(config_dir).unlink(missing_ok=True)
    return f"ключ #{current[0]} отозван"
