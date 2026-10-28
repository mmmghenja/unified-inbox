"""Ключ агента владельца к терминалу (Ф56-5, 2.4 + мелочи раздела 6): `hub term-agent token|key|revoke`.

Ключ — bearer ровно `{term}` (`background`, все проекты). Хранится в `<config_dir>/term-agent.key` (0600,
пишет CLI под владельцем) вместе с номером токена: `{"token_id": N, "key": "hub_…"}`. Агент достаёт ключ командой
`hub term-agent key`, не чтением файла. Повторный `token` отзывает прежний."""

import json
from datetime import datetime
from pathlib import Path

import httpx

from ..agent.files import atomic_write, read_json

KEY_FILE = "term-agent.key"
TOKEN_NAME = "term-agent"  # noqa: S105 — имя токена в ядре, не секрет
EXIT_NO_KEY = 3
NO_KEY_TEXT = "ключа нет: выпусти `hub term-agent token`"


class AgentKeyError(Exception):
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


def issue(admin: httpx.Client, config_dir: Path, *, ttl_days: int | None) -> str:
    previous = read_key(config_dir)
    r = admin.post(
        "/admin/tokens",
        json={
            "name": TOKEN_NAME,
            "kind": "background",
            "scopes": ["term"],
            "project_ids": None,
            "ttl_days": ttl_days,
        },
    )
    if r.status_code == 422:
        raise AgentKeyError(str(r.json().get("detail")))
    if r.status_code != 200:
        raise AgentKeyError(f"ядро не выпустило ключ: HTTP {r.status_code}")
    body = r.json()
    atomic_write(
        key_path(config_dir), json.dumps({"token_id": body["token_id"], "key": body["token"]}) + "\n"
    )
    lines = [f"ключ выпущен #{body['token_id']}, отзыв: hub term-agent revoke"]
    if previous is not None and previous[0] != body["token_id"]:
        rr = admin.post(f"/admin/tokens/{previous[0]}/revoke")
        lines.append(
            f"прежний ключ #{previous[0]} отозван"
            if rr.status_code in (200, 404)
            else f"прежний ключ #{previous[0]} отозвать не удалось (HTTP {rr.status_code})"
        )
    return "\n".join(lines)


def revoke(admin: httpx.Client, config_dir: Path) -> str:
    current = read_key(config_dir)
    if current is None:
        return "ключа нет"
    r = admin.post(f"/admin/tokens/{current[0]}/revoke")
    if r.status_code not in (200, 404):
        raise AgentKeyError(f"ключ #{current[0]} отозвать не удалось (HTTP {r.status_code})")
    key_path(config_dir).unlink(missing_ok=True)
    return f"ключ #{current[0]} отозван"


def format_list(terms: list[dict]) -> str:
    if not terms:
        return "терминалов нет"
    lines = []
    for term in terms:
        owner = "владелец" if term.get("owner") == "ui" else f"агент ({term.get('owner')})"
        created = datetime.fromtimestamp(term["created_ts"] / 1000).strftime("%d.%m %H:%M")
        lines.append(f"#{term['id']}  {term['title']}  {term['cols']}×{term['rows']}  {owner}  с {created}")
    return "\n".join(lines)
