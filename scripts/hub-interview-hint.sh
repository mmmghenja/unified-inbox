#!/usr/bin/env bash
# Ф60-1C: глобальная клавиша «Подскажи» — просит у хаба подсказку по идущему интервью и показывает итог уведомлением.
# Вызывается сочетанием GNOME (его ставит scripts/hub-interview-hotkey.sh). Секретов не читает: токен службы записи
# `hub listen hint` берёт сам.
HUB="${HUB_BIN:-/home/owner/projects/hub-prod/.venv/bin/hub}"

notify() {
  if command -v notify-send >/dev/null 2>&1; then
    notify-send -a "Хаб" -t 3000 "Интервью" "$1" || true
  fi
}

out="$("$HUB" listen hint 2>&1)"
code=$?
case "$code" in
  0) notify "${out:-подсказка запрошена}" ;;
  2) notify "интервью не идёт" ;;
  *) notify "хаб не ответил" ;;
esac
exit "$code"
