#!/usr/bin/env bash
# Uruchamia polecenie Azure CLI z ograniczonym exponential backoff.
#
# Azure Resource Manager sporadycznie zwraca przejściowe 5xx, a Azure CLI może
# wtedy zakończyć się błędem podczas parsowania odpowiedzi HTML. Po wyczerpaniu
# prób zwracany jest kod ostatniego wywołania — błąd nie jest ukrywany.
#
# Użycie:
#   ./azure-cli-retry.sh az containerapp show ...
#
# Konfiguracja (głównie do testów):
#   AZURE_CLI_MAX_ATTEMPTS=4
#   AZURE_CLI_RETRY_DELAY_SECONDS=5
set -euo pipefail

MAX_ATTEMPTS="${AZURE_CLI_MAX_ATTEMPTS:-4}"
RETRY_DELAY="${AZURE_CLI_RETRY_DELAY_SECONDS:-5}"

if [[ "$#" -eq 0 ]]; then
    echo "Usage: $0 <command> [args...]" >&2
    exit 2
fi

if ! [[ "$MAX_ATTEMPTS" =~ ^[1-9][0-9]*$ ]]; then
    echo "AZURE_CLI_MAX_ATTEMPTS must be a positive integer." >&2
    exit 2
fi

if ! [[ "$RETRY_DELAY" =~ ^[0-9]+$ ]]; then
    echo "AZURE_CLI_RETRY_DELAY_SECONDS must be a non-negative integer." >&2
    exit 2
fi

attempt=1
delay="$RETRY_DELAY"

while true; do
    if "$@"; then
        exit 0
    else
        exit_code=$?
    fi

    if (( attempt >= MAX_ATTEMPTS )); then
        echo "::error::Azure CLI command failed after $MAX_ATTEMPTS attempts." >&2
        exit "$exit_code"
    fi

    echo "::warning::Azure CLI command failed (attempt $attempt/$MAX_ATTEMPTS, exit $exit_code). Retrying in ${delay}s." >&2
    sleep "$delay"
    attempt=$((attempt + 1))
    delay=$((delay * 2))
done
