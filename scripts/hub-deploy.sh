#!/usr/bin/env bash
# Обновление боевого хаба до принятого коммита (решение 22.09.2026: разработка и боевой запуск разделены).
#
# Боевые юниты работают из отдельного git worktree ~/projects/hub-prod (ветка `prod`) со своим .venv и своей
# сборкой UI. Разработка идёт в ~/projects/hub и боевой хаб не задевает: editable-установка в dev-дереве больше
# не означает, что перезагрузка ноутбука поднимет недописанный код и применит его миграции к настоящей базе.
#
# ЗАПУСКАЕТ ВЛАДЕЛЕЦ (агент — только по его прямой просьбе). Использование:
#   scripts/hub-deploy.sh [коммит|ветка]      по умолчанию master
#   scripts/hub-deploy.sh --no-backup [...]   без бэкапа перед обновлением (не рекомендуется)
#
# Шаги: бэкап (миграции только вперёд — откат = восстановление из бэкапа) -> fast-forward ветки prod ->
# uv sync -> сборка UI -> установка юнитов из prod-дерева (daemon-reload, рестарт core) -> рестарт активных
# коннекторов -> проверка `hub status`. Любой сбой останавливает скрипт; до шага fast-forward боевое не тронуто.

set -euo pipefail

DEV="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROD="${HUB_PROD_DIR:-$HOME/projects/hub-prod}"

BACKUP=1
SATELLITE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --no-backup) BACKUP=0; shift ;;
        --*) echo "hub-deploy: неизвестный ключ $1" >&2; exit 2 ;;
        *) break ;;
    esac
done
REF="${1:-main}"
PRIMARY_FLAG="$HOME/.config/hub/primary"
if [ "$SATELLITE" = 0 ] && [ ! -e "$PRIMARY_FLAG" ]; then
    if [ ! -e "$HOME/.config/hub/site.json" ]; then
        # Одна машина (нет site.json — схема двух машин не настроена): она и есть главная.
        mkdir -p "$HOME/.config/hub"
        (umask 077 && : >"$PRIMARY_FLAG")
        echo "hub-deploy: одна машина — флаг главной поставлен ($PRIMARY_FLAG)"
    else
        echo "hub-deploy: эта машина не главная (нет $PRIMARY_FLAG)." >&2
        exit 1
    fi
fi

if [ "$(git -C "$PROD" rev-parse --abbrev-ref HEAD 2>/dev/null)" != "prod" ]; then
    echo "hub-deploy: $PROD — не worktree на ветке prod" >&2
    exit 1
fi
if [ -n "$(git -C "$PROD" status --porcelain --untracked-files=no)" ]; then
    echo "hub-deploy: в $PROD есть незакоммиченные правки — боевое дерево руками не правят" >&2
    exit 1
fi

TARGET="$(git -C "$DEV" rev-parse --verify "$REF^{commit}")"
CURRENT="$(git -C "$PROD" rev-parse HEAD)"
if [ "$TARGET" = "$CURRENT" ]; then
    echo "hub-deploy: боевой уже на $(git -C "$DEV" log --oneline -1 "$TARGET")"
    exit 0
fi
if ! git -C "$DEV" merge-base --is-ancestor "$CURRENT" "$TARGET"; then
    echo "hub-deploy: $REF не потомок боевого $CURRENT — только вперёд (откат = восстановление из бэкапа)" >&2
    exit 1
fi

echo "--- что уедет в боевой ---"
git --no-pager -C "$DEV" log --oneline "$CURRENT..$TARGET"

if [ "$BACKUP" = 1 ]; then
    echo "--- бэкап перед обновлением ---"
    "$PROD/.venv/bin/hub" backup
fi

# hub-wake (дежурная, кусок 7c) удалён в Ф57-2: если старый юнит ещё стоит — остановить и снять. Файл юнита
# убирает hub-units-install.sh.
systemctl --user disable --now hub-wake.service 2>/dev/null || true

# hub-work (Ф9-1, 9a, F6): так же — рабочие сессии не должны жить под меняющимся кодом и окружением. Хост при
# остановке закрывает stdin сессиям и гасит их деревья (до 20 с); задачи в ядре — `idle` с событием, после
# старта новая реплика владельца продолжит сессию (`--resume`).
WORK_WAS_ACTIVE=0
work_restore() {
    if [ "$WORK_WAS_ACTIVE" = 1 ]; then
        echo "hub-deploy: сбой — поднимаю hub-work.service обратно" >&2
        if ! systemctl --user start hub-work.service; then
            echo "!!! hub-work остановлен — systemctl --user start hub-work" >&2
        fi
    fi
}
if systemctl --user is-active --quiet hub-work.service; then
    WORK_WAS_ACTIVE=1
    trap work_restore EXIT
    echo "остановка hub-work.service до uv sync"
    systemctl --user stop hub-work.service
fi

echo "--- обновление кода ---"
git -C "$PROD" merge --ff-only --quiet "$TARGET"
(cd "$PROD" && uv sync --frozen --no-dev --quiet)

echo "--- сборка UI ---"
# shellcheck disable=SC1091
source "${NVM_DIR:-$HOME/.nvm}/nvm.sh"
(cd "$PROD/ui" && nvm use >/dev/null && npm ci --silent && npm run build --silent)

echo "--- юниты и рестарт core ---"
bash "$PROD/scripts/hub-units-install.sh"

mapfile -t CONNECTORS < <(systemctl --user list-units 'hub-connector@*' --state=active --plain --no-legend \
    | awk '{print $1}')
for unit in "${CONNECTORS[@]}"; do
    echo "рестарт $unit"
    systemctl --user restart "$unit"
done

# Дежурный агент (кусок 7b): юнит установлен выше, но включает его владелец сам — рестартуем, только если он уже
# работает.
if systemctl --user is-active --quiet hub-agent.service; then
    echo "рестарт hub-agent.service"
    systemctl --user restart hub-agent.service
fi

# Расшифровка голосовых (Ф14-6): так же — только если владелец её уже включил.
if systemctl --user is-active --quiet hub-transcribe.service; then
    echo "рестарт hub-transcribe.service"
    systemctl --user restart hub-transcribe.service
fi

# Запись звонка для помощника на интервью (Ф60-1): так же — только если владелец её уже включил. Посреди звонка
# ядро после своего рестарта ставит интервью на паузу (П6): запись встаёт до «Продолжить» владельца.
if systemctl --user is-active --quiet hub-listen.service; then
    echo "рестарт hub-listen.service"
    systemctl --user restart hub-listen.service
fi

# Спутник уведомлений рабочего стола (Ф33-2): так же — только если владелец его уже включил.
if systemctl --user is-active --quiet hub-notify.service; then
    echo "рестарт hub-notify.service"
    systemctl --user restart hub-notify.service
fi

if [ "$WORK_WAS_ACTIVE" = 1 ]; then
    echo "запуск hub-work.service"
    systemctl --user start hub-work.service
    WORK_WAS_ACTIVE=0  # поднят штатно — trap больше не нужен
fi

echo "--- проверка ---"
for _ in $(seq 1 60); do
    if "$PROD/.venv/bin/hub" status >/dev/null 2>&1; then
        echo "hub-deploy: боевой на $(git -C "$PROD" log --oneline -1)"
        exit 0
    fi
    sleep 1
done
echo "hub-deploy: core не ответил за 60 с — смотри journalctl --user -u hub-core" >&2
exit 1
