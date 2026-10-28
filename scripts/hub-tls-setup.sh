#!/usr/bin/env bash
# https для входа с телефона (Ф14-9, docs/architecture.md: сертификат Let's Encrypt
# на `<домен>` по DNS-01 (TXT `_acme-challenge.<домен>` через API Cloudflare) — прямо на ноутбуке, серверам VPN
# и A-записи это не нужно. Пара ложится в ~/.config/hub/tls/ хуком `hub-tls-deploy-hook.sh`, ядро
# перезапускается и поднимает удалённый слушатель по https. Продление — таймер hub-cert-renew.timer (04:30).
#
# ЗАПУСКАЕТ ТОЛЬКО ВЛАДЕЛЕЦ, после деплоя (юниты hub-cert-renew.* ставит hub-units-install.sh):
#   ~/projects/hub-prod/scripts/hub-tls-setup.sh hub.example.com
#
# Повторный запуск идемпотентен: certbot скажет «рано продлевать», файлы и рестарт — всё равно.
# Токен Cloudflare не печатается и не попадает в аргументы процессов — только в файл через перенаправление.

set -euo pipefail

DOMAIN="${1:-}"
if [[ ! "$DOMAIN" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}$ ]]; then
    echo "использование: $0 <домен>   (например: $0 hub.example.com)" >&2
    exit 2
fi

TLS_DIR="$HOME/.config/hub/tls"
CERTBOT_CONFIG="$TLS_DIR/certbot"
STATE_DIR="$HOME/.local/state/hub/certbot"
CERTBOT="$HOME/.local/bin/certbot"
CF_INI="$TLS_DIR/cloudflare.ini"
FV_TOKEN="$HOME/cf-secrets/.secrets/cf-token"
PROD="$HOME/projects/hub-prod"
HOOK="$PROD/scripts/hub-tls-deploy-hook.sh"
PROD_HUB="$PROD/.venv/bin/hub"

# Каталоги — те же, что `ReadWritePaths=` юнита hub-cert-renew.service (hub-units-install.sh их тоже создаёт).
# shellcheck disable=SC2174 # -m — только последнему каталогу; нужные права ставит chmod ниже
mkdir -p -m 700 "$TLS_DIR" "$STATE_DIR"
chmod 700 "$TLS_DIR" "$STATE_DIR"

if [ ! -x "$HOOK" ] || [ ! -x "$PROD_HUB" ]; then
    echo "hub-tls-setup: нет $HOOK или $PROD_HUB — сначала деплой (scripts/hub-deploy.sh)" >&2
    exit 1
fi
if ! systemctl --user cat hub-cert-renew.timer >/dev/null 2>&1; then
    echo "hub-tls-setup: юнит hub-cert-renew.timer не установлен — сначала деплой (scripts/hub-deploy.sh)" >&2
    exit 1
fi

# Ф14-9 П7: мало бинаря — нужен и плагин dns-cloudflare (строка `* dns-cloudflare` в `certbot plugins`).
# Вывод — в переменную, а не в `| grep -q`: при pipefail ранний выход grep ронял бы certbot по SIGPIPE.
certbot_has_cloudflare() {
    local out
    [ -x "$CERTBOT" ] || return 1
    out="$("$CERTBOT" plugins --config-dir "$CERTBOT_CONFIG" --work-dir "$STATE_DIR" \
        --logs-dir "$STATE_DIR/logs" 2>/dev/null || true)"
    grep -qE '^\* dns-cloudflare[[:space:]]*$' <<< "$out"
}
if ! certbot_has_cloudflare; then
    echo "--- установка certbot с плагином dns-cloudflare (uv tool) ---"
    uv tool install --force certbot --with certbot-dns-cloudflare
fi

if [ ! -s "$CF_INI" ]; then
    use_fv=0
    if [ -r "$FV_TOKEN" ]; then
        read -r -p "Взять токен из ~/cf-secrets/.secrets/cf-token? [Y/n] " answer
        if [[ ! "$answer" =~ ^[Nn] ]]; then
            use_fv=1
        fi
    fi
    (
        umask 077
        if [ "$use_fv" = 1 ]; then
            # П7: одна строка; пробелы, табуляция и \r\n срезаются. Токен — только в памяти и в ini перенаправлением.
            token="$(tr -d '[:space:]' < "$FV_TOKEN")"
            if [ -z "$token" ]; then
                echo "hub-tls-setup: ~/cf-secrets/.secrets/cf-token пуст — положи туда токен Cloudflare" >&2
                exit 1
            fi
        else
            read -rs -p "Токен Cloudflare (API Token, право DNS Edit на зону домена): " token
            echo
            if [ -z "$token" ]; then
                echo "hub-tls-setup: пустой токен" >&2
                exit 1
            fi
        fi
        printf 'dns_cloudflare_api_token = %s\n' "$token" > "$CF_INI.tmp"
        chmod 600 "$CF_INI.tmp"
        mv -f "$CF_INI.tmp" "$CF_INI"
    )
    echo "Учётка Cloudflare записана: $CF_INI (0600)"
else
    echo "Учётка Cloudflare уже есть: $CF_INI (токен неверный — удали этот файл и запусти скрипт снова)"
fi

echo "--- certbot: сертификат на $DOMAIN (DNS-01, Cloudflare) ---"
"$CERTBOT" certonly --non-interactive --agree-tos --register-unsafely-without-email --keep-until-expiring \
    --dns-cloudflare --dns-cloudflare-credentials "$CF_INI" --dns-cloudflare-propagation-seconds 30 \
    -d "$DOMAIN" \
    --config-dir "$CERTBOT_CONFIG" --work-dir "$STATE_DIR" --logs-dir "$STATE_DIR/logs" \
    --deploy-hook "$HOOK"

# certbot зовёт хук только при выпуске; при «рано продлевать» — нет. Файлы и рестарт — в любом случае.
echo "--- копия пары в $TLS_DIR и рестарт ядра ---"
RENEWED_LINEAGE="$CERTBOT_CONFIG/live/$DOMAIN" "$HOOK"

systemctl --user enable --now hub-cert-renew.timer

echo "--- hub remote status ---"
# Ядро только что перезапущено хуком: первые попытки падают «Connection refused» — их трассировки не печатать,
# показать ошибку только если ядро так и не ответило.
status_ok=0
for _ in $(seq 1 20); do
    if "$PROD_HUB" remote status 2>/dev/null; then
        status_ok=1
        break
    fi
    sleep 1
done
if [ "$status_ok" != 1 ]; then
    "$PROD_HUB" remote status || true
fi

echo
echo "С телефона: https://$DOMAIN:8787 (старая закладка http:// больше не откроется)"
