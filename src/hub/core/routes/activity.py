"""`GET /internal/v1/activity` — «Владелец активен в хабе» для коннектора Slack (Ф20-3, A2, B4). Внутренний сокет,
секрет коннектора. Время — из `app.state.activity` (память ядра); `enabled` — выключатель аккаунта
`config_json.slack_status` (JSON `false` — выключено; нет ключа, `true`, битый JSON — включено; как
`read_ack`), читается при каждом вызове, перезапуск коннектора не нужен.

Ф40-3 C: у аккаунта Mattermost выключатель — свой ключ `mm_status` (статус «в сети»), правила те же.
"""

import json

from fastapi import APIRouter, Depends, Request

from .. import connector_auth
from ..connector_auth import ConnectorPrincipal

internal_router = APIRouter(tags=["internal-activity"])

_connector = Depends(connector_auth.connector_dependency())


def status_key(source: str | None) -> str:
    """Ключ выключателя статуса в настройках аккаунта: Mattermost — `mm_status`, прочие — `slack_status`."""
    return "mm_status" if source == "mattermost" else "slack_status"


def slack_status_enabled(config_json: str | None, key: str = "slack_status") -> bool:
    if not config_json:
        return True
    try:
        config = json.loads(config_json)
    except ValueError:
        return True
    return not (isinstance(config, dict) and config.get(key) is False)


@internal_router.get("/activity")
async def get_activity(request: Request, principal: ConnectorPrincipal = _connector) -> dict:
    with request.app.state.db.reader() as conn:
        row = conn.execute(
            "SELECT config_json, source FROM main.account WHERE id = ?", (principal.account_id,)
        ).fetchone()
    return {
        "active_ms": request.app.state.activity.last_ms,
        "now_ms": int(request.app.state.clock() * 1000),
        "enabled": slack_status_enabled(row[0] if row else None, status_key(row[1] if row else None)),
    }
