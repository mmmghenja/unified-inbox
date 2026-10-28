#!/usr/bin/env bash
# Хаб во внешнем Codex владельца (Ф74-7, docs/architecture.md): хуки хаба в ~/.codex/hooks.json
# и MCP `hub-all` в ~/.codex/config.toml. ЗАПУСКАЕТ ТОЛЬКО ВЛАДЕЛЕЦ (агент проверяет скрипт на подменённом HOME,
# настоящий ~/.codex не трогает).
#
#   bash ~/projects/hub-prod/scripts/codex-hub-install.sh
#
# Что делает (идемпотентно — повторный запуск ничего не меняет):
#  1. проверки: есть `codex`, ~/projects/hub-prod/.venv/bin/hub и hub-ext-hook (деплой хаба), профиль MCP `all`
#     (`hub mcp setup`; проверяется только наличие файла, токен не читается);
#  2. MCP `hub-all` в config.toml — только если его там нет (есть — не трогается: его ведёт сисадмин);
#  3. хуки хаба (SessionStart, PreToolUse, PostToolUse, Stop, SessionEnd -> hub-ext-hook codex …) — слиянием:
#     свои записи заменяются, чужие (гард владельца и прочее) остаются как были;
#  4. меняется файл — рядом копия <файл>.bak-<дата>; `codex mcp list` должен показать hub-all — иначе файлы
#     возвращаются из копий и выход с ошибкой;
#  5. подсказка: хуки Codex не доверенные, пока их не одобрить в `/hooks` (один раз; после смены команды хука —
#     снова), и перезапустить окна Codex.
# Секретов скрипт не читает (auth.json, токены — нет).
#
# Переменные (для проверки на копии): HUB_PROD (по умолчанию ~/projects/hub-prod), HUB_CONFIG_DIR
# (~/.config/hub), CODEX_HOME (~/.codex), CODEX_BIN (codex).

set -euo pipefail

PROFILE=all
HUB_PROD="${HUB_PROD:-$HOME/projects/hub-prod}"
HUB_BIN="$HUB_PROD/.venv/bin/hub"
HOOK_BIN="$HUB_PROD/.venv/bin/hub-ext-hook"
PY="$HUB_PROD/.venv/bin/python"
HUB_CONFIG_DIR="${HUB_CONFIG_DIR:-$HOME/.config/hub}"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
CODEX_BIN="${CODEX_BIN:-codex}"
STAMP="$(date +%Y%m%d-%H%M%S)"
export CODEX_HOME

say() { printf '%s\n' "$*"; }
die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }

command -v "$CODEX_BIN" >/dev/null 2>&1 || die "codex не найден в PATH"
version="$("$CODEX_BIN" --version 2>/dev/null | head -n1)" || die "codex --version не отвечает"
[[ -x "$HUB_BIN" ]] || die "нет $HUB_BIN — сначала деплой хаба (scripts/hub-deploy.sh)"
[[ -x "$HOOK_BIN" ]] || die "нет $HOOK_BIN — деплой хаба старее куска 74-7"
[[ -f "$HUB_PROD/deploy/codex/hooks.json" ]] || die "нет $HUB_PROD/deploy/codex/hooks.json — деплой хаба старее куска 74-7"
[[ -f "$HUB_CONFIG_DIR/mcp/$PROFILE.json" ]] || die "нет профиля MCP '$PROFILE' — сначала: $HUB_BIN mcp setup --profile $PROFILE"
say "$version, хаб $HUB_BIN, профиль $PROFILE — есть."

mkdir -p "$CODEX_HOME"
had_config=0; [[ -e "$CODEX_HOME/config.toml" ]] && had_config=1
had_hooks=0; [[ -e "$CODEX_HOME/hooks.json" ]] && had_hooks=1

"$PY" -m hub.engine.ext_install codex --home "$HOME" --hub-prod "$HUB_PROD" --profile "$PROFILE" --stamp "$STAMP" \
    || die "слияние не удалось — файлы не менялись"

rollback() {
    say "Откат: $1"
    for pair in "config.toml:$had_config" "hooks.json:$had_hooks"; do
        f="${pair%%:*}"; had="${pair##*:}"
        if [[ -f "$CODEX_HOME/$f.bak-$STAMP" ]]; then
            cp -p "$CODEX_HOME/$f.bak-$STAMP" "$CODEX_HOME/$f"
            say "  $f возвращён из копии $f.bak-$STAMP"
        elif [[ "$had" == 0 ]]; then
            rm -f "$CODEX_HOME/$f"
        fi
    done
    exit 1
}

listing="$(cd "$HOME" && "$CODEX_BIN" mcp list 2>&1)" || rollback "codex mcp list вышел с ошибкой"
printf '%s\n' "$listing" | grep -q "hub-all" || rollback "codex mcp list не показал hub-all"

say ""
say "Готово. Дальше в каждом окне Codex (один раз):"
say "  1. перезапустить codex;"
say "  2. набрать /hooks и одобрить пять хуков хаба (hub-ext-hook codex start|pre|post|stop|end) — без этого"
say "     Codex их не запускает;"
say "  3. в сессии: «подпишись на проект» (hub_subscribe) — сигналы будут приходить в это окно сами."
