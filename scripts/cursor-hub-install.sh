#!/usr/bin/env bash
# Хаб во внешнем Cursor владельца — IDE и `cursor-agent` (Ф74-3, docs/architecture.md): хуки хаба
# в ~/.cursor/hooks.json и MCP `hub-all` в ~/.cursor/mcp.json. ЗАПУСКАЕТ ТОЛЬКО ВЛАДЕЛЕЦ (агент проверяет скрипт на
# подменённом HOME, настоящий ~/.cursor не трогает).
#
#   bash ~/projects/hub-prod/scripts/cursor-hub-install.sh
#
# Что делает (идемпотентно — повторный запуск ничего не меняет):
#  1. проверки: есть ~/projects/hub-prod/.venv/bin/hub и hub-ext-hook (деплой хаба), профиль MCP `all`
#     (`hub mcp setup`; проверяется только наличие файла, токен не читается);
#  2. MCP `hub-all` в mcp.json — только если его там нет (есть — не трогается: его ведёт сисадмин);
#  3. хуки хаба (sessionStart, preToolUse, postToolUse, stop, sessionEnd -> hub-ext-hook cursor …) — слиянием:
#     свои записи заменяются, чужие (гард владельца и прочее) остаются как были; у `stop` — сутки на ожидание и
#     `loop_limit: null` (сигналы будят окно сколько угодно раз);
#  4. меняется файл — рядом копия <файл>.bak-<дата>; итоговые файлы должны читаться как JSON — иначе возврат
#     из копий и выход с ошибкой;
#  5. подсказка: перезапустить Cursor (IDE) и cursor-agent.
# Секретов скрипт не читает.
#
# Переменные (для проверки на копии): HUB_PROD (по умолчанию ~/projects/hub-prod), HUB_CONFIG_DIR (~/.config/hub).

set -euo pipefail

PROFILE=all
HUB_PROD="${HUB_PROD:-$HOME/projects/hub-prod}"
HUB_BIN="$HUB_PROD/.venv/bin/hub"
HOOK_BIN="$HUB_PROD/.venv/bin/hub-ext-hook"
PY="$HUB_PROD/.venv/bin/python"
HUB_CONFIG_DIR="${HUB_CONFIG_DIR:-$HOME/.config/hub}"
CURSOR_DIR="$HOME/.cursor"
STAMP="$(date +%Y%m%d-%H%M%S)"

say() { printf '%s\n' "$*"; }
die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }

[[ -x "$HUB_BIN" ]] || die "нет $HUB_BIN — сначала деплой хаба (scripts/hub-deploy.sh)"
[[ -x "$HOOK_BIN" ]] || die "нет $HOOK_BIN — деплой хаба старее куска 74-3"
[[ -f "$HUB_PROD/deploy/cursor/hooks.json" ]] || die "нет $HUB_PROD/deploy/cursor/hooks.json — деплой хаба старее куска 74-3"
[[ -f "$HUB_CONFIG_DIR/mcp/$PROFILE.json" ]] || die "нет профиля MCP '$PROFILE' — сначала: $HUB_BIN mcp setup --profile $PROFILE"
if command -v cursor-agent >/dev/null 2>&1; then
    say "cursor-agent $(cursor-agent --version 2>/dev/null | head -n1), хаб $HUB_BIN, профиль $PROFILE — есть."
else
    say "cursor-agent не найден (хуки нужны и IDE), хаб $HUB_BIN, профиль $PROFILE — есть."
fi

mkdir -p "$CURSOR_DIR"
had_mcp=0; [[ -e "$CURSOR_DIR/mcp.json" ]] && had_mcp=1
had_hooks=0; [[ -e "$CURSOR_DIR/hooks.json" ]] && had_hooks=1

"$PY" -m hub.engine.ext_install cursor --home "$HOME" --hub-prod "$HUB_PROD" --profile "$PROFILE" --stamp "$STAMP" \
    || die "слияние не удалось — файлы не менялись"

rollback() {
    say "Откат: $1"
    for pair in "mcp.json:$had_mcp" "hooks.json:$had_hooks"; do
        f="${pair%%:*}"; had="${pair##*:}"
        if [[ -f "$CURSOR_DIR/$f.bak-$STAMP" ]]; then
            cp -p "$CURSOR_DIR/$f.bak-$STAMP" "$CURSOR_DIR/$f"
            say "  $f возвращён из копии $f.bak-$STAMP"
        elif [[ "$had" == 0 ]]; then
            rm -f "$CURSOR_DIR/$f"
        fi
    done
    exit 1
}

"$PY" - "$CURSOR_DIR" <<'PY' || rollback "итоговые файлы Cursor не читаются"
import json, sys
d = sys.argv[1]
mcp = json.load(open(f"{d}/mcp.json"))
assert "hub-all" in (mcp.get("mcpServers") or {}), "нет mcpServers.hub-all"
hooks = json.load(open(f"{d}/hooks.json"))["hooks"]
for event in ("sessionStart", "preToolUse", "postToolUse", "stop", "sessionEnd"):
    assert any("hub-ext-hook cursor " in str(e.get("command")) for e in hooks.get(event) or []), event
PY

say ""
say "Готово. Перезапустите Cursor (IDE) и окна cursor-agent. В IDE: Settings -> MCP — hub-all включён."
say "В чате: «подпишись на проект» (hub_subscribe) — сигналы хаба придут в этот чат после конца хода."
