#!/usr/bin/env bash
# Linkuje Azure Static Web App do Container App backend (idempotent).
#
# Użycie:
#   ./link-swa-backend.sh <prod-app> <rg> <swa-name> <swa-rg> <swa-location>
set -euo pipefail

PROD_APP="${1:?Podaj nazwę Container App (prod)}"
RG="${2:?Podaj resource group Container App}"
SWA_NAME="${3:?Podaj nazwę Static Web App}"
SWA_RG="${4:?Podaj resource group SWA}"
SWA_LOCATION="${5:?Podaj region SWA}"

CONTAINER_APP_ID=$(az containerapp show \
    --name "$PROD_APP" --resource-group "$RG" \
    --query id --output tsv)

# Idempotent: usuń istniejący link (jeśli istnieje) przed ponownym podpięciem.
# Bez tego krok failuje gdy SWA jest już podpięte do innego (np. starego) backendu.
EXISTING=$(az staticwebapp backends list \
    --name "$SWA_NAME" --resource-group "$SWA_RG" \
    --query "[0].backendResourceId" -o tsv 2>/dev/null || true)
if [[ -n "$EXISTING" ]]; then
    echo "Unlinking existing backend: $EXISTING"
    az staticwebapp backends unlink \
        --name           "$SWA_NAME" \
        --resource-group "$SWA_RG"
fi

az staticwebapp backends link \
    --name              "$SWA_NAME" \
    --resource-group    "$SWA_RG" \
    --backend-resource-id "$CONTAINER_APP_ID" \
    --backend-region    "$SWA_LOCATION"

echo "SWA '$SWA_NAME' linked → '$PROD_APP'"
