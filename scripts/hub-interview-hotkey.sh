#!/usr/bin/env bash
# Ф60-1C (`docs/architecture.md`): ставит глобальное сочетание GNOME <Ctrl><Alt>h
# «Подскажи» для помощника на интервью. Запускает ТОЛЬКО владелец (меняет настройки его рабочего стола).
#
# Сочетание вызывает scripts/hub-interview-hint.sh из боевого дерева (абсолютный путь — gsettings `~` не раскрывает),
# оно зовёт `hub listen hint` и показывает итог уведомлением. Повторный запуск не плодит дубль: своя запись ищется
# по имени, чужие сочетания остаются на месте. Снять: `scripts/hub-interview-hotkey.sh --remove`.
set -euo pipefail

NAME="Hub: подсказка на интервью"
BINDING="<Ctrl><Alt>h"
COMMAND="${HUB_INTERVIEW_HINT_CMD:-$HOME/projects/hub-prod/scripts/hub-interview-hint.sh}"
BASE="org.gnome.settings-daemon.plugins.media-keys"
SCHEMA="$BASE.custom-keybinding"
PATH_PREFIX="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings"
MODE="${1:-install}"

command -v gsettings >/dev/null 2>&1 || { echo "gsettings не найден — это не GNOME?" >&2; exit 1; }

# Текущий список путей: `['/a/', '/b/']` или `@as []`.
list="$(gsettings get "$BASE" custom-keybindings)"
paths=()
while IFS= read -r p; do
  [ -n "$p" ] && paths+=("$p")
done < <(printf '%s\n' "$list" | grep -o "'[^']*'" | tr -d "'")

# Наша запись — по имени среди существующих.
ours=""
for p in "${paths[@]}"; do
  n="$(gsettings get "$SCHEMA:$p" name 2>/dev/null || true)"
  if [ "$n" = "'$NAME'" ]; then
    ours="$p"
    break
  fi
done

write_list() {
  local out="[" first=1 p
  for p in "$@"; do
    [ "$first" = 1 ] || out+=", "
    out+="'$p'"
    first=0
  done
  out+="]"
  gsettings set "$BASE" custom-keybindings "$out"
}

if [ "$MODE" = "--remove" ]; then
  if [ -z "$ours" ]; then
    echo "Сочетание не заведено — снимать нечего."
    exit 0
  fi
  rest=()
  for p in "${paths[@]}"; do
    [ "$p" = "$ours" ] || rest+=("$p")
  done
  write_list "${rest[@]}"
  gsettings reset "$SCHEMA:$ours" name
  gsettings reset "$SCHEMA:$ours" command
  gsettings reset "$SCHEMA:$ours" binding
  echo "Сочетание $BINDING снято."
  exit 0
fi

if [ -z "$ours" ]; then
  # Свободный номер customN среди существующих.
  n=0
  while printf '%s\n' "${paths[@]:-}" | grep -qx "$PATH_PREFIX/custom$n/"; do
    n=$((n + 1))
  done
  ours="$PATH_PREFIX/custom$n/"
  paths+=("$ours")
  write_list "${paths[@]}"
  echo "Заведена запись $ours"
else
  echo "Запись уже есть ($ours) — обновляю."
fi

gsettings set "$SCHEMA:$ours" name "$NAME"
gsettings set "$SCHEMA:$ours" command "$COMMAND"
gsettings set "$SCHEMA:$ours" binding "$BINDING"
echo "Готово: $BINDING -> $COMMAND"
if [ ! -x "$COMMAND" ]; then
  echo "Внимание: $COMMAND пока нет или он не исполняемый — после деплоя хаба появится сам." >&2
fi
