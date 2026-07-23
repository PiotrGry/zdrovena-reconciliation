#!/usr/bin/env bash
# Teardown staging: skaluje Container App do 0 replik (brak kosztów między runami PR).
#
# Użycie:
#   ./teardown-staging.sh <app-name> <resource-group>
set -euo pipefail

APP="${1:?Podaj nazwę Container App}"
RG="${2:?Podaj resource group}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"$SCRIPT_DIR/azure-cli-retry.sh" az containerapp update \
    --name           "$APP" \
    --resource-group "$RG" \
    --min-replicas   0 \
    --max-replicas   1 \
    --output none

echo "Staging '$APP' scaled to min=0 max=1 — replicas drop to 0 when idle."
