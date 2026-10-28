#!/usr/bin/env bash
# Generic privacy check of the repository (emails, phones, local IPs, tokens, keys, Russian tax and passport
# numbers, cards, big files, symlinks). Rules, allow-list and markers: scripts/check_private.py.
#   scripts/check-private.sh [DIR]
set -euo pipefail
exec python3 "$(dirname "${BASH_SOURCE[0]}")/check_private.py" "$@"
