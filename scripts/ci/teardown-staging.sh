#!/usr/bin/env bash
# Teardown staging: skaluje Container App do 0 replik (brak kosztów między runami PR).
#
# Użycie:
#   ./teardown-staging.sh <app-name> <resource-group>
set -euo pipefail

APP="${1:?Podaj nazwę Container App}"
RG="${2:?Podaj resource group}"

az containerapp update \
    --name           "$APP" \
    --resource-group "$RG" \
    --min-replicas   0 \
    --max-replicas   0 \
    --output none

echo "Staging '$APP' scaled to 0 — no idle costs."
