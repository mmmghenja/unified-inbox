#!/usr/bin/env bash
# Хук certbot `--deploy-hook` (Ф14-9, docs/architecture.md: новая пара из
# `$RENEWED_LINEAGE` (каталог live/<домен> certbot) -> `<каталог хаба>/tls/{fullchain,privkey}.pem` (0600),
# затем перезапуск ядра: ядро читает пару ТОЛЬКО на старте (перечитывания на ходу нет).
#
# Зовут его certbot (при выпуске и продлении — юнит hub-cert-renew.service) и `scripts/hub-tls-setup.sh`.
# Копирование — сначала обе копии во временные файлы рядом, потом два `mv` подряд (переименование в том же
# каталоге атомарно), рестарт — только после обоих. Ядро не запущено — `try-restart` его не поднимает.

set -euo pipefail
umask 077

if [ -z "${RENEWED_LINEAGE:-}" ]; then
    echo "hub-tls-deploy-hook: нет RENEWED_LINEAGE — хук зовёт certbot (или hub-tls-setup.sh)" >&2
    exit 1
fi

SRC_CERT="$RENEWED_LINEAGE/fullchain.pem"
SRC_KEY="$RENEWED_LINEAGE/privkey.pem"
for src in "$SRC_CERT" "$SRC_KEY"; do
    if [ ! -r "$src" ]; then
        echo "hub-tls-deploy-hook: не читается $src" >&2
        exit 1
    fi
done

TLS_DIR="${HUB_CONFIG_DIR:-$HOME/.config/hub}/tls"
# shellcheck disable=SC2174 # -m — только самому tls/, он и секретный
mkdir -p -m 700 "$TLS_DIR"

TMP_KEY="$(mktemp "$TLS_DIR/.privkey.pem.XXXXXX")"
TMP_CERT="$(mktemp "$TLS_DIR/.fullchain.pem.XXXXXX")"
trap 'rm -f "$TMP_KEY" "$TMP_CERT"' EXIT

# live/<домен>/*.pem у certbot — символические ссылки в archive/: `cat` читает то, на что они указывают.
cat "$SRC_KEY" > "$TMP_KEY"
cat "$SRC_CERT" > "$TMP_CERT"
chmod 600 "$TMP_KEY" "$TMP_CERT"

mv -f "$TMP_KEY" "$TLS_DIR/privkey.pem"
mv -f "$TMP_CERT" "$TLS_DIR/fullchain.pem"

systemctl --user try-restart hub-core.service
