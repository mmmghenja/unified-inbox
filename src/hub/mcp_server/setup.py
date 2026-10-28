"""`hub mcp setup --profile NAME [--projects all|1,2,3] [--organize]`. Спека 7a, B4 и П7.

Запускает владелец (боевой `~/.config/hub` агенту закрыт). Через админский сокет создаёт токен
`kind=interactive`, `name="mcp:<профиль>"`, скоупы `read draft` (+`organize`), без срока; атомарно пишет
файлы профиля; и только ПОТОМ отзывает прежний токен профиля — и только если его имя ровно `mcp:<профиль>`
(испорченный `.json` не должен отозвать чужой токен). Значение токена не печатается и не попадает в
аргументы командной строки — только в файл.
"""

import json
import os
import shutil
import sys
from pathlib import Path

import httpx

from . import profile as profile_mod


class SetupError(Exception):
    """Понятная ошибка настройки — CLI печатает её и выходит с ненулевым кодом."""


def parse_projects(value: str) -> list[int] | None:
    if value.strip() == "all":
        return None
    try:
        ids = sorted({int(part) for part in value.split(",") if part.strip()})
    except ValueError as exc:
        raise SetupError(f"--projects: 'all' или список id через запятую, получено {value!r}") from exc
    if not ids:
        raise SetupError("--projects: пустой список проектов")
    return ids


def hub_executable() -> str:
    """Абсолютный путь к скрипту `hub` того окружения, из которого запущен setup (для боевого дерева —
    `~/projects/hub-prod/.venv/bin/hub`). Каталог интерпретатора — без `resolve()`: `.venv/bin/python` —
    символическая ссылка на общий Python, а нужен именно скрипт этого окружения."""
    candidate = Path(os.path.abspath(sys.executable)).parent / "hub"
    if candidate.exists():
        return str(candidate)
    found = shutil.which("hub")
    if found:
        return os.path.abspath(found)
    return str(candidate)


def _old_token_to_revoke(admin: httpx.Client, config_dir: Path, profile: str) -> int | None:
    meta = profile_mod.read_meta(config_dir, profile)
    old_id = meta.get("token_id") if meta else None
    if not isinstance(old_id, int):
        return None
    r = admin.get("/admin/tokens")
    r.raise_for_status()
    for token in r.json():
        if token["id"] == old_id and token["name"] == f"mcp:{profile}" and not token["revoked"]:
            return old_id
    return None


def run_attach(admin: httpx.Client, *, config_dir: Path, profile: str, on: bool) -> str:
    """Ф14-3 C: `hub mcp attach --profile NAME on|off` — дописать/убрать скоуп `attach` у действующего токена
    профиля (`token_id` из описания, ручка `POST /admin/tokens/{id}/attach`) без перевыпуска; при успехе —
    `scopes` в описании профиля."""
    try:
        profile_mod.validate_profile(profile)
    except profile_mod.ProfileError as exc:
        raise SetupError(str(exc)) from exc
    meta = profile_mod.read_meta(config_dir, profile)
    token_id = meta.get("token_id") if meta else None
    if not isinstance(token_id, int) or isinstance(token_id, bool):
        raise SetupError(f"профиль {profile} не настроен: hub mcp setup --profile {profile}")
    r = admin.post(f"/admin/tokens/{token_id}/attach", json={"on": on})
    if r.status_code != 200:
        raise SetupError(f"ядро отказало: HTTP {r.status_code} {r.text}")
    scopes = r.json().get("scopes")
    if not isinstance(scopes, list):
        scopes = [s for s in meta.get("scopes") or [] if s != "attach"] + (["attach"] if on else [])
    profile_mod.write_meta(config_dir, profile, {**meta, "scopes": scopes})
    state = "выдано" if on else "снято"
    return (
        f"Профиль {profile}: право прикладывать файлы {state} (токен id={token_id}, "
        f"скоупы {' '.join(scopes)}). Токен тот же; перезапусти сессии Claude, чтобы они увидели "
        "параметр files."
    )


def run_setup(
    admin: httpx.Client,
    *,
    config_dir: Path,
    profile: str,
    projects: str = "all",
    organize: bool = False,
    hub_path: str | None = None,
    attach: bool = False,
) -> str:
    """Возвращает текст для владельца (команды подключения). Ошибки ввода — `SetupError` ДО создания токена."""
    try:
        profile_mod.validate_profile(profile)
    except profile_mod.ProfileError as exc:
        raise SetupError(str(exc)) from exc
    project_ids = parse_projects(projects)
    if organize and project_ids is not None:
        raise SetupError(
            "--organize только для профиля на все проекты (--projects all): раскладка меняет проект канала"
        )
    scopes = ["read", "draft"] + (["organize"] if organize else [])
    if attach:
        scopes.append("attach")  # Ф14-3 C: файлы в черновиках (ядро само откажет несовместимым)

    old_id = _old_token_to_revoke(admin, config_dir, profile)

    r = admin.post(
        "/admin/tokens",
        json={
            "name": f"mcp:{profile}",
            "kind": "interactive",
            "scopes": scopes,
            "project_ids": project_ids,
            "ttl_days": None,
        },
    )
    if r.status_code != 200:
        raise SetupError(f"ядро не создало токен: HTTP {r.status_code} {r.text}")
    body = r.json()
    profile_mod.write_profile(
        config_dir,
        profile,
        token=body["token"],
        meta={"token_id": body["token_id"], "projects": project_ids, "scopes": scopes},
    )

    revoked_note = ""
    if old_id is not None and old_id != body["token_id"]:
        # Ф92-1 (B3): перевыпуск, а не отзыв — подписки сессий прежнего токена возвращаются под новым.
        rr = admin.post(f"/admin/tokens/{old_id}/revoke", json={"replaced_by": body["token_id"]})
        if rr.status_code == 200:
            revoked_note = f"Прежний токен профиля (id={old_id}) отозван.\n"
        else:
            revoked_note = f"Прежний токен id={old_id} отозвать не удалось (HTTP {rr.status_code}).\n"

    hub = hub_path or hub_executable()
    name = f"hub-{profile}"
    command = [hub, "mcp", "--profile", profile]
    opencode = {"mcp": {name: {"type": "local", "command": command}}}
    scope_text = "все проекты" if project_ids is None else "проекты " + ", ".join(map(str, project_ids))
    lines = [
        f"Профиль {profile}: токен id={body['token_id']}, скоупы {' '.join(scopes)}, {scope_text}.",
        f"Токен записан в {profile_mod.token_path(config_dir, profile)} (0600).",
    ]
    if revoked_note:
        lines.append(revoked_note.rstrip("\n"))
    lines += [
        "",
        "Claude Code (все сессии пользователя; вместо -s user можно -s project в нужной папке):",
        f"  claude mcp add -s user {name} -- {' '.join(command)}",
        "",
        "OpenCode (фрагмент opencode.json):",
        "  " + json.dumps(opencode, ensure_ascii=False),
        "  Внимание: профиль для внешних моделей — только проекты, чью переписку можно отдавать внешнему "
        "провайдеру.",
        "",
        "Черновики агента ждут подтверждения в хабе (очередь «Ждёт подтверждения»); сам агент ничего не "
        "отправляет.",
    ]
    return "\n".join(lines)
