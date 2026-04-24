#!/usr/bin/env bash
# Czeka na zakończenie provisioningu Azure Container App.
#
# Użycie:
#   ./wait-containerapp.sh <app-name> <resource-group> [max-attempts] [sleep-sec]
#
# Zwraca 0 gdy stan == Succeeded, 1 przy Failed lub timeout.
set -euo pipefail

APP="${1:?Podaj nazwę Container App}"
RG="${2:?Podaj resource group}"
MAX="${3:-36}"
SLEEP="${4:-10}"

for i in $(seq 1 "$MAX"); do
    STATE=$(az containerapp show \
        --name "$APP" --resource-group "$RG" \
        --query "properties.provisioningState" -o tsv 2>/dev/null || echo "Unknown")
    echo "[$i/$MAX] $APP → $STATE"
    [[ "$STATE" == "Succeeded" ]] && exit 0
    [[ "$STATE" == "Failed" ]] && { echo "Container App in Failed state"; exit 1; }
    sleep "$SLEEP"
done

echo "Timeout: $APP still not Succeeded after $MAX attempts"
exit 1
