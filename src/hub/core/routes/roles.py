"""Ф41-8: роли хаба для интерфейса. Только кука интерфейса; только чтение — тексты ролей правятся в коде
(`src/hub/roles/*.md`), ручек записи нет.

- `GET /roles` — каталог без текстов: `[{"key", "title", "names", "sub", "compact_tokens",
  "compact_auto"}]`.
- `GET /roles/{key}` — одна роль с текстом: те же поля и `text` — та самая вставка, что получает сессия с
  этой ролью (`roles.prompt`: общие правила взаимодействия + текст роли). Неизвестный ключ — 404.

Ф48-2 (D): `compact_tokens` / `compact_auto` — умолчания сжатия для новых сессий роли (`null` — не заданы)."""

from fastapi import APIRouter, HTTPException

from hub import roles as roles_mod

from ..security import Principal
from .write import ui_only

public_router = APIRouter(tags=["roles"])


@public_router.get("/roles")
async def get_roles(principal: Principal = ui_only) -> list[dict]:
    return [_brief(r) for r in roles_mod.catalog()]


def _brief(role) -> dict:
    return {
        "key": role.key,
        "title": role.title,
        "names": list(role.names),
        "sub": role.sub,
        "compact_tokens": role.compact_tokens,
        "compact_auto": role.compact_auto,
    }


@public_router.get("/roles/{key}")
async def get_role(key: str, principal: Principal = ui_only) -> dict:
    found = next((r for r in roles_mod.catalog() if r.key == key), None)
    if found is None:
        raise HTTPException(404, "not found")
    return {**_brief(found), "text": roles_mod.prompt(found.names[0])}
