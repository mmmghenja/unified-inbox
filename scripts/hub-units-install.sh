#!/usr/bin/env bash
# Установка юнитов systemd --user хаба. Спецификация: docs/architecture.md
#
# ЗАПУСКАЕТ ТОЛЬКО ВЛАДЕЛЕЦ. Агенту боевые каталоги (~/.local/share/hub, ~/.config/hub,
# ~/.config/systemd/user) закрыты — этот скрипт он готовит, но не исполняет.
#
# Идемпотентен: повторный запуск безопасен (копирует поверх, `daemon-reload`, `enable --now` не падает на
# уже включённых юнитах). Юнит-шаблон коннектора (hub-connector@.service, кусок 1a фазы 1) копируется, но
# НЕ enable --now: нужен конкретный account_id, владелец включает его сам после `hub auth tg`.

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/deploy/systemd"
DEST_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

UNITS=(
    hub-core.service
    hub-watch.service
    hub-watch.timer
    hub-backup.service
    hub-backup.timer
)

# Юнит-шаблон коннектора (кусок 1a фазы 1) — копируется, но НЕ enable --now: нужен конкретный account_id
# (владелец заводит его сам командой `hub auth tg`, скрипт печатает готовую команду включения в конце).
TEMPLATE_UNITS=(
    hub-connector@.service
)

# Дежурный агент (кусок 7b; Ф57-1 — без модели) — копируется, но НЕ enable --now: включает владелец после
# `hub agent setup` (скрипт печатает команду в конце).
# hub-wake (дежурная, кусок 7c) удалён в Ф57-2 — старый юнит снимается ниже.
# hub-work (Ф9-1, 9a) — так же: включает владелец после `hub work setup` (он же пишет файл окружения юнита).
# hub-tunnel@ (Ф10-7, доступ с телефона) — юнит-шаблон туннеля; включает владелец после системной части VPN.
# hub-transcribe (Ф14-6) — расшифровка голосовых; включает владелец после `hub transcribe setup`.
# hub-listen (Ф60-1) — запись звонка для помощника на интервью; включает владелец после `hub listen setup`.
# hub-notify (Ф33-2) — уведомления рабочего стола из ядра; включает владелец после `hub notify setup`.
# hub-cert-renew (Ф14-9) — продление сертификата https для телефона; таймер включает hub-tls-setup.sh.
MANUAL_UNITS=(
    hub-agent.service
    hub-work.service
    hub-tunnel@.service
    hub-transcribe.service
    hub-listen.service
    hub-notify.service
    hub-cert-renew.service
    hub-cert-renew.timer
)

# Каталоги данных и конфигурации ОБЯЗАНЫ существовать до первого старта: `ReadWritePaths=` в песочнице юнита
# монтирует их заранее, и на чистой машине core падал с 226/NAMESPACE («No such file or directory»), не успев
# создать их сам. Пути — те же буквальные, что в юнитах (%h/.local/share/hub, %h/.config/hub).
# Кусок 7b, К4: `ReadWritePaths=%h/.local/share/hub/agent` юнита hub-agent — тот же случай.
# Ф14-6 П11: `ReadWritePaths=%h/.local/share/hub/transcribe` юнита hub-transcribe — тоже.
# Ф14-9 П2: `ReadWritePaths=%h/.config/hub/tls %h/.local/state/hub/certbot` юнита hub-cert-renew — тоже.
# Ф60-1: `ReadWritePaths=%h/.local/share/hub/listen` юнита hub-listen — тоже.
mkdir -p -m 700 "$HOME/.local/share/hub" "$HOME/.config/hub" "$HOME/.local/share/hub/agent" "$HOME/.local/share/hub/transcribe" "$HOME/.local/share/hub/listen" "$HOME/.config/hub/tls" "$HOME/.local/state/hub/certbot"
chmod 700 "$HOME/.local/share/hub" "$HOME/.config/hub" "$HOME/.local/share/hub/agent" "$HOME/.local/share/hub/transcribe" "$HOME/.local/share/hub/listen" "$HOME/.config/hub/tls" "$HOME/.local/state/hub/certbot"

# Юниты запускают код из боевого worktree (решение 22.09.2026, scripts/hub-deploy.sh). Без него core не
# стартует вовсе — лучше отказать здесь, чем оставить хаб лежать после `restart`.
PROD_HUB="$HOME/projects/hub-prod/.venv/bin/hub"
if [ ! -x "$PROD_HUB" ]; then
    echo "hub-units-install: нет $PROD_HUB — сначала боевой worktree (см. scripts/hub-deploy.sh)" >&2
    exit 1
fi

mkdir -p "$DEST_DIR"
for unit in "${UNITS[@]}" "${TEMPLATE_UNITS[@]}" "${MANUAL_UNITS[@]}"; do
    cp "$SRC_DIR/$unit" "$DEST_DIR/$unit"
done

# Ф57-2: дежурной hub-wake больше нет — снять старый юнит, если он остался с прежних выкладок.
systemctl --user disable --now hub-wake.service 2>/dev/null || true
rm -f "$DEST_DIR/hub-wake.service"

systemctl --user daemon-reload

# Ф33-4 (docs/architecture.md п.33-4.1): юниты площадки идут только при флаге главной. Без флага
# юниты ставятся и включаются (поднимутся, когда флаг появится), но ничего не останавливается и не стартует.
# Одна машина (нет site.json — схема двух машин не настроена) — она и есть главная: флаг ставится сам.
PRIMARY_FLAG="$HOME/.config/hub/primary"
if [ ! -e "$PRIMARY_FLAG" ] && [ ! -e "$HOME/.config/hub/site.json" ]; then
    (umask 077 && : >"$PRIMARY_FLAG")
    echo "hub-units-install: одна машина — флаг главной поставлен ($PRIMARY_FLAG)"
fi
if [ -e "$PRIMARY_FLAG" ]; then
    systemctl --user reset-failed hub-core.service 2>/dev/null || true
    systemctl --user enable hub-core.service
    # Ответило ли ядро — проверяет hub-deploy.sh (`hub status`).
    systemctl --user restart --no-block hub-core.service
    systemctl --user enable --now hub-watch.timer
    systemctl --user enable --now hub-backup.timer
    echo "--- systemctl --user status ---"
    systemctl --user status hub-core.service hub-watch.timer hub-backup.timer --no-pager || true
else
    systemctl --user enable hub-core.service hub-watch.timer hub-backup.timer
    echo
    echo "ЭТА МАШИНА НЕ ГЛАВНАЯ (нет $PRIMARY_FLAG): юниты площадки установлены, но не запущены."
fi

RECIPIENT_FILE="${HUB_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/hub}/backup.recipient"
if [ ! -s "$RECIPIENT_FILE" ]; then
    echo
    echo "ВНИМАНИЕ: получатель бэкапа не задан — бэкапы НЕ делаются, и hub-watch об этом молчит."
    echo "  age-keygen -o key.txt   (key.txt унести с ноутбука)   ->   hub backup --set-recipient age1..."
fi

echo
echo "Юнит коннектора (hub-connector@.service) скопирован; включает владелец по account_id после \`hub auth tg|slack\`:"
echo "  systemctl --user enable --now hub-connector@<account_id>.service"
echo
echo "Юнит дежурного агента (hub-agent.service) скопирован; включает владелец после \`hub agent setup\`:"
echo "  systemctl --user enable --now hub-agent.service"
echo
echo "Юнит hub-work (hub-work.service) скопирован; включает владелец после \`hub work setup\`:"
echo "  systemctl --user enable --now hub-work.service"
echo
echo "Юнит расшифровки голосовых (hub-transcribe.service) скопирован; включает владелец после \`hub transcribe setup\`:"
echo "  systemctl --user enable --now hub-transcribe.service"
echo
echo "Юнит записи интервью (hub-listen.service) скопирован; включает владелец после \`hub listen setup\`:"
echo "  systemctl --user enable --now hub-listen.service"
echo
echo "Юнит уведомлений рабочего стола (hub-notify.service) скопирован; включает владелец после \`hub notify setup\`:"
echo "  systemctl --user enable --now hub-notify.service"
echo
echo "Юнит туннеля доступа с телефона (hub-tunnel@.service) скопирован; включает владелец после системной части"
echo "(DNS, обратный прокси) и включения доступа в хабе:"
echo "  systemctl --user enable --now hub-tunnel@<ssh-алиас сервера>"
echo
echo "Юниты продления сертификата https (hub-cert-renew.service/.timer) скопированы; таймер включает"
echo "~/projects/hub-prod/scripts/hub-tls-setup.sh <домен> (он же получает первый сертификат)."
echo
