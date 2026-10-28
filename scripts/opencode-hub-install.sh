#!/usr/bin/env bash
# Хаб в OpenCode владельца (Ф16-4, docs/architecture.md): MCP `hub-all`, агент
# `hub-project`, плагин хаба. ЗАПУСКАЕТ ТОЛЬКО ВЛАДЕЛЕЦ (агент готовит скрипт и проверяет его на копии конфига
# с подменённым HOME, настоящий ~/.config/opencode не трогает).
#
#   bash ~/projects/hub-prod/scripts/opencode-hub-install.sh
#
# Что делает (идемпотентно — повторный запуск ничего не ломает):
#  1. проверки: `opencode --version` не ниже 1.18, есть ~/projects/hub-prod/.venv/bin/hub, есть профиль MCP
#     `all` (`hub mcp setup` делал владелец; проверяется только наличие файла, токен не читается);
#  2. копия ~/.config/opencode/opencode.jsonc -> opencode.jsonc.bak-<ГГГГММДД-ЧЧММСС>;
#  3. MCP `hub-all` в объект "mcp" — текстовой вставкой (комментарии jsonc сохраняются), с "timeout" 180000 мс:
#     по умолчанию OpenCode обрывает вызов MCP через 60 с (проба P4), а hub_wait держит до 120 с. Уже есть
#     `hub-all` без "timeout" — дописывается только "timeout", остальное не трогается;
#  4. агент ~/.config/opencode/agents/hub-project.md (DeepSeek Flash, variant max) — если его нет;
#  5. симлинк ~/.config/opencode/plugins/hub.ts -> ~/projects/hub-prod/deploy/opencode/hub.ts (деплой хаба
#     обновляет плагин сам);
#  6. `opencode debug config` должен выйти 0 и показать `hub-all` с "timeout" — иначе конфиг возвращается из
#     копии, созданное этим запуском убирается, выход с ошибкой;
#  7. подсказка: перезапустить OpenCode (TUI и `opencode serve` на 4096).
# Секретов скрипт не читает (auth.json, server.env, токены — нет).
#
# Переменные (для проверки на копии): HUB_PROD (дерево боевого хаба, по умолчанию ~/projects/hub-prod),
# HUB_CONFIG_DIR (каталог конфига хаба, по умолчанию ~/.config/hub), XDG_CONFIG_HOME, OPENCODE_BIN.

set -euo pipefail

MIN_MAJOR=1
MIN_MINOR=18
MCP_TIMEOUT_MS=180000
PROFILE=all

HUB_PROD="${HUB_PROD:-$HOME/projects/hub-prod}"
HUB_BIN="$HUB_PROD/.venv/bin/hub"
PLUGIN_SRC="$HUB_PROD/deploy/opencode/hub.ts"
HUB_CONFIG_DIR="${HUB_CONFIG_DIR:-$HOME/.config/hub}"
OC_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/opencode"
OPENCODE_BIN="${OPENCODE_BIN:-opencode}"
STAMP="$(date +%Y%m%d-%H%M%S)"

say() { printf '%s\n' "$*"; }
die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }

# --- 1. проверки -----------------------------------------------------------------------------------------------

command -v "$OPENCODE_BIN" >/dev/null 2>&1 || die "opencode не найден в PATH"
version="$("$OPENCODE_BIN" --version 2>/dev/null | head -n1 | tr -d '[:space:]')" || die "opencode --version не отвечает"
if [[ ! "$version" =~ ^([0-9]+)\.([0-9]+) ]]; then
    die "не разобрать версию OpenCode: '$version'"
fi
major="${BASH_REMATCH[1]}"
minor="${BASH_REMATCH[2]}"
if (( major < MIN_MAJOR || (major == MIN_MAJOR && minor < MIN_MINOR) )); then
    die "нужен OpenCode ${MIN_MAJOR}.${MIN_MINOR} или новее, стоит $version"
fi
[[ -x "$HUB_BIN" ]] || die "нет $HUB_BIN — сначала деплой хаба (scripts/hub-deploy.sh)"
[[ -f "$PLUGIN_SRC" ]] || die "нет $PLUGIN_SRC — деплой хаба старее куска 16-4"
[[ -f "$HUB_CONFIG_DIR/mcp/$PROFILE.json" ]] || die "нет профиля MCP '$PROFILE' — сначала: $HUB_BIN mcp setup --profile $PROFILE"
say "OpenCode $version, хаб $HUB_BIN, профиль $PROFILE — есть."

mkdir -p "$OC_DIR" "$OC_DIR/agents" "$OC_DIR/plugins"

CONFIG="$OC_DIR/opencode.jsonc"
if [[ ! -f "$CONFIG" && -f "$OC_DIR/opencode.json" ]]; then
    CONFIG="$OC_DIR/opencode.json"
fi

CREATED_CONFIG=0
CREATED_AGENT=0
CREATED_LINK=0
BACKUP=""
PREV_LINK_BACKUP=""

rollback() {
    say "Откат: $1"
    if [[ -n "$BACKUP" ]]; then
        cp -p "$BACKUP" "$CONFIG"
        say "  конфиг возвращён из $BACKUP"
    elif (( CREATED_CONFIG )); then
        rm -f "$CONFIG"
    fi
    (( CREATED_AGENT )) && rm -f "$OC_DIR/agents/hub-project.md"
    if (( CREATED_LINK )); then
        rm -f "$OC_DIR/plugins/hub.ts"
        [[ -n "$PREV_LINK_BACKUP" ]] && mv "$PREV_LINK_BACKUP" "$OC_DIR/plugins/hub.ts"
    fi
    exit 1
}

# --- 2. копия конфига -----------------------------------------------------------------------------------------

if [[ -f "$CONFIG" ]]; then
    BACKUP="$CONFIG.bak-$STAMP"
    cp -p "$CONFIG" "$BACKUP"
    say "Копия конфига: $BACKUP"
else
    printf '{\n  "$schema": "https://opencode.ai/config.json"\n}\n' > "$CONFIG"
    CREATED_CONFIG=1
    say "Конфига не было — создан $CONFIG"
fi

# --- 3. MCP hub-all (текстовая правка jsonc, комментарии сохраняются) ---------------------------------------------

if ! python3 - "$CONFIG" "$HUB_BIN" "$PROFILE" "$MCP_TIMEOUT_MS" <<'PY'
import json
import sys

path, hub_bin, profile, timeout_ms = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
text = open(path, encoding="utf-8").read()


def tokens(src):
    """Значимые лексемы jsonc вне строк и комментариев: (вид, начало, конец, значение строки)."""
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == '"':
            j = i + 1
            while j < n and src[j] != '"':
                j += 2 if src[j] == "\\" else 1
            yield ("str", i, j + 1, json.loads(src[i : j + 1]))
            i = j + 1
        elif src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c in "{}[]:,":
            yield (c, i, i + 1, None)
            i += 1
        elif c.isspace():
            i += 1
        else:
            j = i
            while j < n and src[j] not in "{}[]:,\"/" and not src[j].isspace():
                j += 1
            yield ("lit", i, j, None)
            i = j


toks = list(tokens(text))


def skip_value(k):
    """Индекс лексемы после значения, начинающегося с toks[k]."""
    kind = toks[k][0]
    if kind in "{[":
        depth = 0
        while True:
            if toks[k][0] in "{[":
                depth += 1
            elif toks[k][0] in "}]":
                depth -= 1
                if depth == 0:
                    return k + 1
            k += 1
    return k + 1


def members(k):
    """Члены объекта с `{` в toks[k]: [(ключ, индекс лексемы ключа, индекс начала значения)], индекс `}`."""
    out = []
    k += 1
    while toks[k][0] != "}":
        if toks[k][0] == ",":
            k += 1
            continue
        key_k = k
        value_k = k + 2  # ключ, `:`, значение
        out.append((toks[key_k][3], key_k, value_k))
        k = skip_value(value_k)
    return out, k


def indent_before(pos):
    line_start = text.rfind("\n", 0, pos) + 1
    head = text[line_start:pos]
    return head if head.strip() == "" else None


def insert_member(obj_k, entry, fallback_indent):
    """Вставить `entry` первым членом объекта (после `{`) с отступом первого члена."""
    global text
    mems, _close = members(obj_k)
    brace = toks[obj_k][2]
    if mems:
        ind = indent_before(toks[mems[0][1]][1])
        if ind is None:
            text = text[:brace] + " " + entry + "," + text[brace:]
        else:
            text = text[:brace] + "\n" + ind + entry + "," + text[brace:]
    else:
        text = text[:brace] + "\n" + fallback_indent + entry + text[brace:]


top = next(k for k, t in enumerate(toks) if t[0] == "{")
top_mems, _ = members(top)
mcp = next((m for m in top_mems if m[0] == "mcp"), None)
entry_obj = {"type": "local", "command": [hub_bin, "mcp", "--profile", profile], "enabled": True, "timeout": timeout_ms}
entry = '"hub-all": ' + json.dumps(entry_obj, ensure_ascii=False)
if mcp is None:
    insert_member(top, '"mcp": {' + entry + "}", "  ")
    print("  mcp.hub-all: добавлен (объекта mcp не было)")
elif toks[mcp[2]][0] != "{":
    sys.exit("в конфиге 'mcp' — не объект; правьте вручную")
else:
    mcp_mems, _ = members(mcp[2])
    hub = next((m for m in mcp_mems if m[0] == "hub-all"), None)
    if hub is None:
        insert_member(mcp[2], entry, "    ")
        print("  mcp.hub-all: добавлен")
    elif toks[hub[2]][0] != "{":
        sys.exit("в конфиге 'mcp.hub-all' — не объект; правьте вручную")
    else:
        hub_mems, _ = members(hub[2])
        if any(m[0] == "timeout" for m in hub_mems):
            print("  mcp.hub-all: уже есть, timeout задан — не трогаю")
        else:
            insert_member(hub[2], f'"timeout": {timeout_ms}', "      ")
            print(f"  mcp.hub-all: уже был — дописан timeout {timeout_ms}")

tmp = path + ".tmp-hub"
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write(text)
import os

os.replace(tmp, path)
PY
then
    rollback "правка конфига не удалась"
fi

# --- 4. агент hub-project -------------------------------------------------------------------------------------

AGENT="$OC_DIR/agents/hub-project.md"
if [[ -e "$AGENT" ]]; then
    say "  агент hub-project: уже есть — не трогаю"
else
    cat > "$AGENT" <<'MD'
---
description: Сессия проекта хаба (подписка на проект, сигналы, черновики, реакции)
mode: primary
model: deepseek/deepseek-flash
variant: max
---
Сессия проекта хаба. Правила проекта — CLAUDE.md и .claude/hub.md проекта.
MD
    CREATED_AGENT=1
    say "  агент hub-project: создан"
fi

# --- 5. плагин хаба -------------------------------------------------------------------------------------------

LINK="$OC_DIR/plugins/hub.ts"
if [[ -L "$LINK" && "$(readlink "$LINK")" == "$PLUGIN_SRC" ]]; then
    say "  плагин: симлинк уже на месте"
else
    if [[ -e "$LINK" || -L "$LINK" ]]; then
        PREV_LINK_BACKUP="$LINK.bak-$STAMP"
        mv "$LINK" "$PREV_LINK_BACKUP"
        say "  плагин: прежний файл отложен в $PREV_LINK_BACKUP"
    fi
    ln -s "$PLUGIN_SRC" "$LINK"
    CREATED_LINK=1
    say "  плагин: $LINK -> $PLUGIN_SRC"
fi

# --- 6. проверка итогового конфига ----------------------------------------------------------------------------

# Из HOME и без проектного конфига: проверяется глобальный конфиг, а не opencode.json текущей папки.
if ! out="$(cd "$HOME" && OPENCODE_DISABLE_PROJECT_CONFIG=1 OPENCODE_DISABLE_AUTOUPDATE=1 "$OPENCODE_BIN" debug config 2>/dev/null)"; then
    rollback "opencode debug config вышел с ошибкой"
fi
if ! printf '%s' "$out" | python3 -c '
import json, sys
cfg = json.load(sys.stdin)
hub = (cfg.get("mcp") or {}).get("hub-all") or {}
if not hub:
    sys.exit("нет mcp.hub-all")
if not isinstance(hub.get("timeout"), int):
    sys.exit("у mcp.hub-all нет timeout")
cmd = hub.get("command") or []
if hub.get("enabled") is False:
    print("  ВНИМАНИЕ: mcp.hub-all выключен у вас в конфиге (enabled: false) — включите его сами")
if cmd[:1] != [sys.argv[1]]:
    print("  ВНИМАНИЕ: mcp.hub-all запускает " + " ".join(map(str, cmd)) + " (ожидался " + sys.argv[1] + ")")
' "$HUB_BIN"; then
    rollback "в итоговом конфиге OpenCode нет hub-all с timeout"
fi

say ""
say "Готово. Перезапустите OpenCode (TUI и opencode serve на 4096 — как вы его запускаете обычно)."
say "Сессия проекта: агент hub-project в рабочей папке проекта -> «подпишись на проект» (hub_subscribe)."
